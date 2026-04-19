from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
import json
import queue
from typing import Any, Iterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .service import RealtimeTranscriptionService, ServiceState


def _encode_sse(payload: dict[str, object]) -> bytes:
    event_type = str(payload.get("type", "message"))
    data = json.dumps(payload, ensure_ascii=False)
    return f"event: {event_type}\ndata: {data}\n\n".encode("utf-8")


def _list_audio_devices() -> dict[str, Any]:
    try:
        import pyaudiowpatch as pyaudio

        p = pyaudio.PyAudio()
        try:
            wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
            devices: list[dict[str, Any]] = []
            for i in range(p.get_device_count()):
                info = p.get_device_info_by_index(i)
                host_api = info.get("hostApi", -1)
                if host_api != wasapi["index"]:
                    continue
                devices.append({
                    "index": int(info["index"]),
                    "name": str(info["name"]),
                    "sample_rate": int(round(float(info["defaultSampleRate"]))),
                    "input_channels": int(info.get("maxInputChannels", 0)),
                    "output_channels": int(info.get("maxOutputChannels", 0)),
                    "is_loopback": bool(info.get("isLoopbackDevice", False)),
                })

            default_mic_idx = int(wasapi.get("defaultInputDevice", -1))
            default_speaker_idx = int(wasapi.get("defaultOutputDevice", -1))
            return {
                "devices": devices,
                "default_mic_index": default_mic_idx,
                "default_speaker_index": default_speaker_idx,
            }
        finally:
            p.terminate()
    except ImportError as exc:
        return {
            "devices": [],
            "default_mic_index": None,
            "default_speaker_index": None,
            "error": f"Audio library not available: {exc}",
        }
    except Exception as exc:
        return {
            "devices": [],
            "default_mic_index": None,
            "default_speaker_index": None,
            "error": f"Audio device enumeration failed: {exc}",
        }


def create_app(
    service: RealtimeTranscriptionService | None = None,
) -> FastAPI:
    from .persistence import SessionStore

    if service is not None:
        active_service = service
        session_store = getattr(service, "session_store", None)
        if session_store is None:
            session_store = SessionStore()
            active_service.session_store = session_store
    else:
        session_store = SessionStore()
        active_service = RealtimeTranscriptionService(session_store=session_store)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            active_service.stop()
            if session_store is not None:
                session_store.close()

    app = FastAPI(title="Groq Whisper Service", lifespan=lifespan)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse(active_service.health())

    @app.get("/state")
    def state() -> JSONResponse:
        return JSONResponse(active_service.snapshot())

    @app.get("/events")
    def events() -> StreamingResponse:
        subscriber = active_service.subscribe()

        def stream() -> Iterator[bytes]:
            try:
                while True:
                    try:
                        payload = subscriber.get(timeout=15.0)
                    except queue.Empty:
                        yield b": keep-alive\n\n"
                        continue
                    yield _encode_sse(payload)
            finally:
                active_service.unsubscribe(subscriber)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/start")
    async def start(request: Request) -> JSONResponse:
        body: dict[str, Any] = {}
        if request.headers.get("content-type", "").startswith("application/json"):
            try:
                body = await request.json()
            except Exception:
                pass
        if "api_key_file" in body:
            return JSONResponse(
                {"ok": False, "error": "api_key_file is no longer supported. Save the API keys in the Windows app settings."},
                status_code=400,
            )
        if "api_key" in body:
            return JSONResponse(
                {"ok": False, "error": "api_key is no longer supported. Send api_keys in POST /start instead."},
                status_code=400,
            )
        api_keys = body.pop("api_keys", None)
        if (
            not isinstance(api_keys, list)
            or any(not isinstance(item, str) for item in api_keys)
            or not any(item.strip() for item in api_keys)
        ):
            return JSONResponse(
                {"ok": False, "error": "Missing API keys. Save at least one key in Settings and try again."},
                status_code=400,
            )
        if body:
            result = active_service.update_config(body)
            if not result["ok"]:
                return JSONResponse(result, status_code=409)
        result = active_service.start(api_keys=api_keys)
        status_code = 200 if result["ok"] else 409
        return JSONResponse(result, status_code=status_code)

    @app.post("/stop")
    def stop() -> JSONResponse:
        result = active_service.stop()
        status_code = 200 if result["ok"] else 409
        return JSONResponse(result, status_code=status_code)

    @app.post("/pause")
    def pause() -> JSONResponse:
        result = active_service.pause()
        status_code = 200 if result["ok"] else 409
        return JSONResponse(result, status_code=status_code)

    @app.post("/resume")
    def resume() -> JSONResponse:
        result = active_service.resume()
        status_code = 200 if result["ok"] else 409
        return JSONResponse(result, status_code=status_code)

    @app.get("/devices")
    def devices() -> JSONResponse:
        return JSONResponse(_list_audio_devices())

    @app.get("/settings")
    def get_settings() -> JSONResponse:
        return JSONResponse(asdict(active_service.config))

    @app.put("/settings")
    async def put_settings(request: Request) -> JSONResponse:
        body = await request.json()
        secret_fields = [field for field in ("api_key", "api_key_file", "api_keys") if field in body]
        if secret_fields:
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"{', '.join(secret_fields)} is no longer accepted in /settings. Save the API keys in the Windows app settings and send them only in POST /start.",
                },
                status_code=400,
            )
        result = active_service.update_config(body)
        status_code = 200 if result["ok"] else 409
        return JSONResponse(result, status_code=status_code)

    @app.get("/sessions")
    def list_sessions(limit: int = 50, offset: int = 0) -> JSONResponse:
        rows = session_store.list_sessions(limit=limit, offset=offset)
        return JSONResponse({"sessions": rows})

    @app.get("/sessions/{session_id}")
    def get_session(session_id: str) -> JSONResponse:
        row = session_store.get_session(session_id)
        if row is None:
            return JSONResponse({"error": "Session not found"}, status_code=404)
        return JSONResponse(row)

    @app.patch("/sessions/{session_id}")
    async def patch_session(session_id: str, request: Request) -> JSONResponse:
        body = await request.json()
        row = session_store.get_session(session_id)
        if row is None:
            return JSONResponse({"error": "Session not found"}, status_code=404)
        export_path = body.get("export_path")
        if export_path is not None:
            session_store.update_export_path(session_id, export_path)
        return JSONResponse({"ok": True})

    @app.delete("/sessions/{session_id}")
    def delete_session(session_id: str) -> JSONResponse:
        snapshot = active_service.snapshot()
        if snapshot.get("session_id") == session_id and snapshot.get("state") in ("running", "paused"):
            return JSONResponse({"error": "Cannot delete active session"}, status_code=409)
        deleted = session_store.delete_session(session_id)
        if not deleted:
            return JSONResponse({"error": "Session not found"}, status_code=404)
        return JSONResponse({"ok": True})

    return app


app = create_app()

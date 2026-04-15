from __future__ import annotations

from contextlib import asynccontextmanager
import json
import queue
from typing import Iterator

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse

from .service import RealtimeTranscriptionService


def _encode_sse(payload: dict[str, object]) -> bytes:
    event_type = str(payload.get("type", "message"))
    data = json.dumps(payload, ensure_ascii=False)
    return f"event: {event_type}\ndata: {data}\n\n".encode("utf-8")


def create_app(
    service: RealtimeTranscriptionService | None = None,
) -> FastAPI:
    active_service = service or RealtimeTranscriptionService()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            active_service.stop()

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
    def start() -> JSONResponse:
        result = active_service.start()
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

    return app


app = create_app()

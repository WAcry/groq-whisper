from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import io
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol
import wave

import numpy as np

from .rolling_transcriber import (
    DEFAULT_GRANULARITIES,
    DEFAULT_MODEL,
    create_client,
    load_api_key,
    transcribe_bytes,
)
from .stable_prefix import AggregatorConfig, PatchEvent, StablePrefixAggregator


DEFAULT_CAPTURE_POLL_INTERVAL_SECONDS = 0.25
DEFAULT_CAPTURE_RETENTION_SECONDS = 120.0


class ServiceState(str, Enum):
    idle = "idle"
    preflight = "preflight"
    running = "running"
    paused = "paused"
    error = "error"


class AudioWindowLike(Protocol):
    audio: np.ndarray
    sample_rate: int
    start_time: float
    end_time: float

    @property
    def duration_seconds(self) -> float:
        ...


class AudioCaptureLike(Protocol):
    capture_started_at: float | None

    def start(self) -> None:
        ...

    def stop(self) -> None:
        ...

    def snapshot_mixed_window(
        self,
        *,
        window_seconds: float,
        end_time: float | None = None,
    ) -> AudioWindowLike:
        ...


CaptureFactory = Callable[["RealtimeTranscriptionServiceConfig"], AudioCaptureLike]


@dataclass(frozen=True)
class RealtimeTranscriptionServiceConfig:
    model: str = DEFAULT_MODEL
    prompt: str | None = None
    language: str | None = None
    window_seconds: float = 30.0
    hop_seconds: float = 5.0
    commit_lag_seconds: float = 7.0
    poll_interval_seconds: float = DEFAULT_CAPTURE_POLL_INTERVAL_SECONDS
    capture_retention_seconds: float = DEFAULT_CAPTURE_RETENTION_SECONDS
    api_key_file: Path | None = None


def build_default_capture(
    config: RealtimeTranscriptionServiceConfig,
) -> AudioCaptureLike:
    from .audio_capture import ContinuousDualAudioCapture

    return ContinuousDualAudioCapture(
        poll_interval_seconds=config.poll_interval_seconds,
        retention_seconds=max(config.capture_retention_seconds, config.window_seconds * 2.0),
    )


def encode_audio_window_to_flac_bytes(
    audio: np.ndarray,
    *,
    sample_rate: int,
) -> bytes:
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim != 2:
        raise ValueError("Expected audio with shape=(frames, channels)")

    clipped = np.clip(audio, -1.0, 1.0)
    pcm16 = np.round(clipped * 32767.0).astype(np.int16)
    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, "wb") as wav_file:
        wav_file.setnchannels(pcm16.shape[1] if pcm16.ndim == 2 else 1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm16.tobytes())

    command = [
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "wav",
        "-i",
        "pipe:0",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-map",
        "0:a",
        "-c:a",
        "flac",
        "-f",
        "flac",
        "pipe:1",
    ]
    result = subprocess.run(
        command,
        input=wav_buffer.getvalue(),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg failed while encoding the live window: {stderr}")
    if not result.stdout:
        raise RuntimeError("ffmpeg produced no FLAC bytes for the live window.")
    return result.stdout


class SessionStoreLike(Protocol):
    def create_session(self, *, model: str, language: str | None, prompt: str | None) -> str: ...
    def update_text(self, session_id: str, *, full_text: str, tick_count: int) -> None: ...
    def finalize_session(self, session_id: str, **kwargs: Any) -> None: ...


class RealtimeTranscriptionService:
    def __init__(
        self,
        config: RealtimeTranscriptionServiceConfig | None = None,
        *,
        capture_factory: CaptureFactory | None = None,
        api_key_loader: Callable[[Path | None], str] = load_api_key,
        client_factory: Callable[[str], Any] = create_client,
        transcribe_func: Callable[..., dict[str, Any]] = transcribe_bytes,
        clock: Callable[[], float] = time.perf_counter,
        session_store: SessionStoreLike | None = None,
    ) -> None:
        self.config = config or RealtimeTranscriptionServiceConfig()
        if self.config.window_seconds <= 0.0:
            raise ValueError("window_seconds must be positive")
        if self.config.hop_seconds <= 0.0:
            raise ValueError("hop_seconds must be positive")
        if self.config.commit_lag_seconds < 0.0:
            raise ValueError("commit_lag_seconds must be non-negative")

        self.capture_factory = capture_factory or build_default_capture
        self.api_key_loader = api_key_loader
        self.client_factory = client_factory
        self.transcribe_func = transcribe_func
        self.clock = clock
        self.session_store = session_store

        self.state_lock = threading.Lock()
        self.subscribers_lock = threading.Lock()
        self.stop_event = threading.Event()
        self._paused = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self.capture: AudioCaptureLike | None = None
        self.client: Any = None
        self.started_at_monotonic: float | None = None
        self.latest_patch_payload: dict[str, Any] | None = None
        self.last_error: str | None = None
        self._state: ServiceState = ServiceState.idle
        self.running = False
        self.subscribers: set[queue.Queue[dict[str, Any]]] = set()
        self._preflight_results: dict[str, Any] | None = None
        self._current_session_id: str | None = None

        self.aggregator = self._build_aggregator()

    def _build_aggregator(self) -> StablePrefixAggregator:
        return StablePrefixAggregator(
            AggregatorConfig(
                window_seconds=self.config.window_seconds,
                hop_seconds=self.config.hop_seconds,
                commit_lag_seconds=self.config.commit_lag_seconds,
            )
        )

    @staticmethod
    def _validate_config(config: RealtimeTranscriptionServiceConfig) -> list[str]:
        errors = []
        if config.window_seconds <= 0.0:
            errors.append("window_seconds must be positive")
        if config.hop_seconds <= 0.0:
            errors.append("hop_seconds must be positive")
        if config.commit_lag_seconds < 0.0:
            errors.append("commit_lag_seconds must be non-negative")
        return errors

    def update_config(self, overrides: dict[str, Any]) -> dict[str, Any]:
        with self.state_lock:
            if self._state not in (ServiceState.idle, ServiceState.error):
                return {
                    "ok": False,
                    "state": self._state.value,
                    "error": "Settings can only be changed when idle",
                }
            allowed_fields = {
                "model", "prompt", "language", "window_seconds",
                "hop_seconds", "commit_lag_seconds", "api_key_file",
            }
            current = asdict(self.config)
            for key, value in overrides.items():
                if key in allowed_fields:
                    if key == "api_key_file" and value is not None:
                        value = Path(value)
                    current[key] = value
            candidate = RealtimeTranscriptionServiceConfig(**current)
            validation_errors = self._validate_config(candidate)
            if validation_errors:
                return {
                    "ok": False,
                    "state": self._state.value,
                    "error": "; ".join(validation_errors),
                }
            self.config = candidate
            return {"ok": True, "state": self._state.value}

    def _preflight(self) -> dict[str, Any]:
        results: dict[str, Any] = {
            "api_key": False,
            "ffmpeg": False,
            "errors": [],
        }
        try:
            self.api_key_loader(self.config.api_key_file)
            results["api_key"] = True
        except Exception as exc:
            results["errors"].append(f"API key: {exc}")
        try:
            proc = subprocess.run(
                ["ffmpeg", "-version"],
                capture_output=True,
                timeout=5.0,
            )
            results["ffmpeg"] = proc.returncode == 0
            if proc.returncode != 0:
                results["errors"].append("ffmpeg returned non-zero exit code")
        except FileNotFoundError:
            results["errors"].append("ffmpeg not found on PATH")
        except Exception as exc:
            results["errors"].append(f"ffmpeg check: {exc}")
        return results

    def start(self) -> dict[str, Any]:
        with self.state_lock:
            if self._state not in (ServiceState.idle, ServiceState.error):
                return {
                    "ok": False,
                    "state": self._state.value,
                    "error": f"Cannot start from state '{self._state.value}'",
                }
            if self.worker_thread is not None and self.worker_thread.is_alive():
                return {
                    "ok": False,
                    "state": self._state.value,
                    "error": "Previous worker thread is still alive",
                }
            self._state = ServiceState.preflight

        preflight = self._preflight()
        self._preflight_results = preflight

        if preflight["errors"]:
            with self.state_lock:
                self._state = ServiceState.error
                self.last_error = "; ".join(preflight["errors"])
            return {
                "ok": False,
                "state": ServiceState.error.value,
                "error": self.last_error,
            }

        try:
            self.stop_event.clear()
            self._paused.clear()
            self.last_error = None
            self.latest_patch_payload = None
            self.aggregator = self._build_aggregator()
            self.started_at_monotonic = self.clock()
            api_key = self.api_key_loader(self.config.api_key_file)
            self.client = self.client_factory(api_key)
            self.capture = self.capture_factory(self.config)
            self.capture.start()
        except Exception as exc:
            if self.capture is not None:
                try:
                    self.capture.stop()
                except Exception:
                    pass
                self.capture = None
            self.client = None
            with self.state_lock:
                self._state = ServiceState.error
                self.last_error = str(exc)
            return {
                "ok": False,
                "state": ServiceState.error.value,
                "error": self.last_error,
            }

        with self.state_lock:
            self._state = ServiceState.running
            self.running = True

        if self.session_store is not None:
            try:
                self._current_session_id = self.session_store.create_session(
                    model=self.config.model,
                    language=self.config.language,
                    prompt=self.config.prompt,
                )
            except Exception:
                self._current_session_id = None

        self.worker_thread = threading.Thread(target=self._run_loop, daemon=True)
        self.worker_thread.start()
        self._publish(
            {
                "type": "service.ready",
                "model": self.config.model,
                "window_seconds": self.config.window_seconds,
                "hop_seconds": self.config.hop_seconds,
                "commit_lag_seconds": self.config.commit_lag_seconds,
            }
        )
        return {"ok": True, "state": ServiceState.running.value}

    def stop(self) -> dict[str, Any]:
        with self.state_lock:
            if self._state not in (ServiceState.running, ServiceState.paused):
                return {
                    "ok": False,
                    "state": self._state.value,
                    "error": f"Cannot stop from state '{self._state.value}'",
                }

        self._paused.clear()
        self.stop_event.set()
        worker = self.worker_thread
        if worker is not None and worker.is_alive():
            worker.join(timeout=15.0)
            if worker.is_alive():
                with self.state_lock:
                    self._state = ServiceState.error
                    self.last_error = "Worker thread did not stop within timeout"
                return {
                    "ok": False,
                    "state": ServiceState.error.value,
                    "error": self.last_error,
                }
        elif self.capture is not None:
            self.capture.stop()
            self.capture = None
        self.worker_thread = None
        self._finalize_dangling_session()
        with self.state_lock:
            self.running = False
            self._state = ServiceState.idle
        return {"ok": True, "state": ServiceState.idle.value}

    def _finalize_dangling_session(self) -> None:
        if self.session_store is None or self._current_session_id is None:
            return
        try:
            session = self.session_store.get_session(self._current_session_id)
            if session is not None and session.get("ended_at") is None:
                duration_s = None
                if self.started_at_monotonic is not None:
                    duration_s = self.clock() - self.started_at_monotonic
                self.session_store.finalize_session(
                    self._current_session_id,
                    duration_seconds=duration_s,
                )
        except Exception:
            pass
        self._current_session_id = None

    def pause(self) -> dict[str, Any]:
        with self.state_lock:
            if self._state != ServiceState.running:
                return {
                    "ok": False,
                    "state": self._state.value,
                    "error": f"Cannot pause from state '{self._state.value}'",
                }
            self._paused.set()
            self._state = ServiceState.paused
        self._publish({"type": "service.paused"})
        return {"ok": True, "state": ServiceState.paused.value}

    def resume(self) -> dict[str, Any]:
        with self.state_lock:
            if self._state != ServiceState.paused:
                return {
                    "ok": False,
                    "state": self._state.value,
                    "error": f"Cannot resume from state '{self._state.value}'",
                }
            self._paused.clear()
            self._state = ServiceState.running
        self._publish({"type": "service.resumed"})
        return {"ok": True, "state": ServiceState.running.value}

    def snapshot(self) -> dict[str, Any]:
        with self.state_lock:
            return {
                "state": self._state.value,
                "running": self.running,
                "started_at_monotonic": self.started_at_monotonic,
                "error": self.last_error,
                "latest_patch": self.latest_patch_payload,
                "preflight_results": self._preflight_results,
                "model": self.config.model,
            }

    def health(self) -> dict[str, Any]:
        return {"status": "ok"}

    def subscribe(self, *, replay_latest: bool = True) -> queue.Queue[dict[str, Any]]:
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=64)
        with self.subscribers_lock:
            self.subscribers.add(subscriber)
        if replay_latest and self.latest_patch_payload is not None:
            subscriber.put_nowait(self.latest_patch_payload)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any]]) -> None:
        with self.subscribers_lock:
            self.subscribers.discard(subscriber)

    def _publish(self, payload: dict[str, Any]) -> None:
        payload = dict(payload)
        event_type = payload.get("type")
        if event_type in {"transcription.patch", "transcription.final"}:
            with self.state_lock:
                self.latest_patch_payload = payload
            self._persist_event(payload)

        with self.subscribers_lock:
            subscribers = list(self.subscribers)
        for subscriber in subscribers:
            if subscriber.full():
                try:
                    subscriber.get_nowait()
                except queue.Empty:
                    pass
            try:
                subscriber.put_nowait(payload)
            except queue.Full:
                pass

    def _publish_error(self, message: str) -> None:
        with self.state_lock:
            self.last_error = message
        self._publish({"type": "service.error", "message": message})
        self._persist_error(message)

    def _persist_event(self, payload: dict[str, Any]) -> None:
        if self.session_store is None or self._current_session_id is None:
            return
        event_type = payload.get("type")
        try:
            if event_type == "transcription.patch":
                self.session_store.update_text(
                    self._current_session_id,
                    full_text=payload.get("display_text", ""),
                    tick_count=payload.get("tick_index", 0),
                )
            elif event_type == "transcription.final":
                duration_s = None
                if self.started_at_monotonic is not None:
                    duration_s = self.clock() - self.started_at_monotonic
                self.session_store.finalize_session(
                    self._current_session_id,
                    full_text=payload.get("display_text", ""),
                    tick_count=payload.get("tick_index", 0),
                    duration_seconds=duration_s,
                )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Session persistence failed: %s", exc)

    def _persist_error(self, message: str) -> None:
        if self.session_store is None or self._current_session_id is None:
            return
        try:
            duration_s = None
            if self.started_at_monotonic is not None:
                duration_s = self.clock() - self.started_at_monotonic
            self.session_store.finalize_session(
                self._current_session_id,
                error_log=message,
                duration_seconds=duration_s,
            )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Session error persistence failed: %s", exc)

    def _build_patch_payload(
        self,
        patch_event: PatchEvent,
        *,
        tick_index: int,
        window_start_s: float,
        window_end_s: float,
        audio_duration_s: float,
        event_type: str,
    ) -> dict[str, Any]:
        payload = asdict(patch_event)
        payload.update(
            {
                "type": event_type,
                "tick_index": tick_index,
                "window_start_s": window_start_s,
                "window_end_s": window_end_s,
                "audio_duration_s": audio_duration_s,
            }
        )
        return payload

    def _run_loop(self) -> None:
        tick_index = 1
        try:
            if self.capture is None or self.started_at_monotonic is None:
                raise RuntimeError("The live transcription service was not initialized")

            while not self.stop_event.is_set():
                if self._paused.is_set():
                    self.stop_event.wait(0.5)
                    continue

                tick_end_monotonic = (
                    self.started_at_monotonic + (tick_index * self.config.hop_seconds)
                )
                delay_s = tick_end_monotonic - self.clock()
                if delay_s > 0.0:
                    self.stop_event.wait(min(delay_s, 0.5))
                    continue

                window = self.capture.snapshot_mixed_window(
                    window_seconds=self.config.window_seconds,
                    end_time=tick_end_monotonic,
                )
                audio_bytes = encode_audio_window_to_flac_bytes(
                    window.audio,
                    sample_rate=window.sample_rate,
                )
                transcription = self.transcribe_func(
                    self.client,
                    f"live-tick-{tick_index:04d}.flac",
                    audio_bytes,
                    model=self.config.model,
                    prompt=self.config.prompt,
                    language=self.config.language,
                    granularities=DEFAULT_GRANULARITIES,
                )
                window_end_s = tick_index * self.config.hop_seconds
                window_start_s = max(0.0, window_end_s - self.config.window_seconds)
                patch_event = self.aggregator.ingest(
                    transcription,
                    window_end_s=window_end_s,
                )
                self._publish(
                    self._build_patch_payload(
                        patch_event,
                        tick_index=tick_index,
                        window_start_s=window_start_s,
                        window_end_s=window_end_s,
                        audio_duration_s=window.duration_seconds,
                        event_type="transcription.patch",
                    )
                )
                tick_index += 1
        except Exception as exc:
            self._publish_error(str(exc))
            with self.state_lock:
                self._state = ServiceState.error
        finally:
            try:
                if tick_index > 1:
                    final_event = self.aggregator.flush()
                    final_tick_index = max(0, tick_index - 1)
                    final_window_end_s = final_tick_index * self.config.hop_seconds
                    final_window_start_s = max(
                        0.0,
                        final_window_end_s - self.config.window_seconds,
                    )
                    self._publish(
                        self._build_patch_payload(
                            final_event,
                            tick_index=final_tick_index,
                            window_start_s=final_window_start_s,
                            window_end_s=final_window_end_s,
                            audio_duration_s=max(0.0, final_window_end_s - final_window_start_s),
                            event_type="transcription.final",
                        )
                    )
            finally:
                if self.capture is not None:
                    self.capture.stop()
                    self.capture = None
                with self.state_lock:
                    self.running = False
                    if self._state == ServiceState.running:
                        self._state = ServiceState.idle

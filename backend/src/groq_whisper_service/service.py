from __future__ import annotations

from dataclasses import asdict, dataclass
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

        self.state_lock = threading.Lock()
        self.subscribers_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self.capture: AudioCaptureLike | None = None
        self.client: Any = None
        self.started_at_monotonic: float | None = None
        self.latest_patch_payload: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.running = False
        self.subscribers: set[queue.Queue[dict[str, Any]]] = set()

        self.aggregator = self._build_aggregator()

    def _build_aggregator(self) -> StablePrefixAggregator:
        return StablePrefixAggregator(
            AggregatorConfig(
                window_seconds=self.config.window_seconds,
                hop_seconds=self.config.hop_seconds,
                commit_lag_seconds=self.config.commit_lag_seconds,
            )
        )

    def start(self) -> None:
        if self.worker_thread is not None and self.worker_thread.is_alive():
            return

        self.stop_event.clear()
        self.last_error = None
        self.latest_patch_payload = None
        self.aggregator = self._build_aggregator()
        self.started_at_monotonic = self.clock()
        api_key = self.api_key_loader(self.config.api_key_file)
        self.client = self.client_factory(api_key)
        self.capture = self.capture_factory(self.config)
        self.capture.start()

        with self.state_lock:
            self.running = True

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

    def stop(self) -> None:
        self.stop_event.set()
        worker = self.worker_thread
        if worker is not None and worker.is_alive():
            worker.join(timeout=15.0)
        elif self.capture is not None:
            self.capture.stop()
            self.capture = None
        self.worker_thread = None
        with self.state_lock:
            self.running = False

    def snapshot(self) -> dict[str, Any]:
        with self.state_lock:
            return {
                "running": self.running,
                "started_at_monotonic": self.started_at_monotonic,
                "error": self.last_error,
                "latest_patch": self.latest_patch_payload,
            }

    def health(self) -> dict[str, Any]:
        snapshot = self.snapshot()
        status = "ok" if snapshot["running"] and snapshot["error"] is None else "degraded"
        return {
            "status": status,
            "running": snapshot["running"],
            "error": snapshot["error"],
        }

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

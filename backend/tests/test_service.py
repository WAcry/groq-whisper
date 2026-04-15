from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

import numpy as np


ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from groq_whisper_service.service import (
    RealtimeTranscriptionService,
    RealtimeTranscriptionServiceConfig,
    ServiceState,
)


@dataclass(frozen=True)
class FakeWindow:
    audio: np.ndarray
    sample_rate: int
    start_time: float
    end_time: float

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.end_time - self.start_time)


class FakeCapture:
    def __init__(self, config: RealtimeTranscriptionServiceConfig) -> None:
        self.config = config
        self.capture_started_at: float | None = None
        self.stopped = False

    def start(self) -> None:
        self.capture_started_at = time.perf_counter()

    def stop(self) -> None:
        self.stopped = True

    def snapshot_mixed_window(
        self,
        *,
        window_seconds: float,
        end_time: float | None = None,
    ) -> FakeWindow:
        assert self.capture_started_at is not None
        effective_end_time = end_time if end_time is not None else time.perf_counter()
        start_time = max(self.capture_started_at, effective_end_time - window_seconds)
        duration_seconds = max(0.0, effective_end_time - start_time)
        frames = max(1, int(round(duration_seconds * 48_000)))
        audio = np.zeros((frames, 2), dtype=np.float32)
        return FakeWindow(
            audio=audio,
            sample_rate=48_000,
            start_time=start_time,
            end_time=effective_end_time,
        )


def make_transcription(word: str) -> dict[str, object]:
    return {
        "text": word,
        "words": [
            {
                "word": word,
                "start": 0.0,
                "end": 0.02,
            }
        ],
        "segments": [
            {
                "id": 0,
                "seek": 0,
                "start": 0.0,
                "end": 0.02,
                "text": word,
                "avg_logprob": -0.1,
                "compression_ratio": 1.1,
                "no_speech_prob": 0.01,
            }
        ],
    }


class RealtimeServiceTests(unittest.TestCase):
    def test_service_emits_patch_events(self) -> None:
        config = RealtimeTranscriptionServiceConfig(
            window_seconds=0.10,
            hop_seconds=0.05,
            commit_lag_seconds=0.0,
        )
        service = RealtimeTranscriptionService(
            config,
            capture_factory=FakeCapture,
            api_key_loader=lambda _: "test-key",
            client_factory=lambda _: object(),
            transcribe_func=lambda *args, **kwargs: make_transcription("alpha"),
        )

        with mock.patch(
            "groq_whisper_service.service.encode_audio_window_to_flac_bytes",
            return_value=b"audio",
        ):
            service.start()
            subscriber = service.subscribe(replay_latest=False)
            patch_events: list[dict[str, object]] = []

            deadline = time.perf_counter() + 1.0
            while time.perf_counter() < deadline and len(patch_events) < 3:
                payload = subscriber.get(timeout=0.2)
                if payload.get("type") == "transcription.patch":
                    patch_events.append(payload)

            service.stop()
            final_events: list[dict[str, object]] = []
            drain_deadline = time.perf_counter() + 0.5
            while time.perf_counter() < drain_deadline:
                try:
                    payload = subscriber.get(timeout=0.1)
                except Exception:
                    break
                if payload.get("type") == "transcription.final":
                    final_events.append(payload)
                    break
            service.unsubscribe(subscriber)

        self.assertGreaterEqual(len(patch_events), 2)
        self.assertEqual(patch_events[0]["tail_text"], "alpha")
        self.assertTrue(final_events)
        self.assertEqual(final_events[-1]["committed_text"], "alpha")
        self.assertIsNone(service.snapshot()["error"])


def _make_service(**overrides):
    config = RealtimeTranscriptionServiceConfig(
        window_seconds=0.10,
        hop_seconds=0.05,
        commit_lag_seconds=0.0,
    )
    defaults = dict(
        capture_factory=FakeCapture,
        api_key_loader=lambda _: "test-key",
        client_factory=lambda _: object(),
        transcribe_func=lambda *args, **kwargs: make_transcription("hello"),
    )
    defaults.update(overrides)
    return RealtimeTranscriptionService(config, **defaults)


class StateTransitionTests(unittest.TestCase):
    def test_initial_state_is_idle(self) -> None:
        service = _make_service()
        self.assertEqual(service._state, ServiceState.idle)
        snapshot = service.snapshot()
        self.assertEqual(snapshot["state"], "idle")

    def test_health_returns_ok_regardless_of_state(self) -> None:
        service = _make_service()
        self.assertEqual(service.health(), {"status": "ok"})

    def test_start_transitions_to_running(self) -> None:
        service = _make_service()
        with mock.patch(
            "groq_whisper_service.service.encode_audio_window_to_flac_bytes",
            return_value=b"audio",
        ):
            result = service.start()
            self.assertTrue(result["ok"])
            self.assertEqual(result["state"], "running")
            self.assertEqual(service._state, ServiceState.running)
            service.stop()

    def test_start_from_running_fails(self) -> None:
        service = _make_service()
        with mock.patch(
            "groq_whisper_service.service.encode_audio_window_to_flac_bytes",
            return_value=b"audio",
        ):
            service.start()
            result = service.start()
            self.assertFalse(result["ok"])
            self.assertEqual(result["state"], "running")
            service.stop()

    def test_stop_from_idle_fails(self) -> None:
        service = _make_service()
        result = service.stop()
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "idle")

    def test_pause_resume_cycle(self) -> None:
        service = _make_service()
        with mock.patch(
            "groq_whisper_service.service.encode_audio_window_to_flac_bytes",
            return_value=b"audio",
        ):
            service.start()
            pause_result = service.pause()
            self.assertTrue(pause_result["ok"])
            self.assertEqual(pause_result["state"], "paused")
            self.assertEqual(service._state, ServiceState.paused)

            resume_result = service.resume()
            self.assertTrue(resume_result["ok"])
            self.assertEqual(resume_result["state"], "running")
            self.assertEqual(service._state, ServiceState.running)

            service.stop()

    def test_pause_from_idle_fails(self) -> None:
        service = _make_service()
        result = service.pause()
        self.assertFalse(result["ok"])

    def test_resume_from_running_fails(self) -> None:
        service = _make_service()
        with mock.patch(
            "groq_whisper_service.service.encode_audio_window_to_flac_bytes",
            return_value=b"audio",
        ):
            service.start()
            result = service.resume()
            self.assertFalse(result["ok"])
            service.stop()

    def test_stop_from_paused_succeeds(self) -> None:
        service = _make_service()
        with mock.patch(
            "groq_whisper_service.service.encode_audio_window_to_flac_bytes",
            return_value=b"audio",
        ):
            service.start()
            service.pause()
            result = service.stop()
            self.assertTrue(result["ok"])
            self.assertEqual(result["state"], "idle")

    def test_preflight_failure_transitions_to_error(self) -> None:
        def bad_key_loader(_):
            raise ValueError("bad key")

        service = _make_service(api_key_loader=bad_key_loader)
        result = service.start()
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "error")
        self.assertIn("API key", result["error"])

    def test_start_from_error_state_succeeds(self) -> None:
        call_count = 0

        def flaky_key_loader(_):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                raise ValueError("bad key")
            return "test-key"

        service = _make_service(api_key_loader=flaky_key_loader)
        with mock.patch(
            "groq_whisper_service.service.encode_audio_window_to_flac_bytes",
            return_value=b"audio",
        ):
            result1 = service.start()
            self.assertFalse(result1["ok"])
            self.assertEqual(service._state, ServiceState.error)

            result2 = service.start()
            self.assertTrue(result2["ok"])
            self.assertEqual(service._state, ServiceState.running)
            service.stop()

    def test_snapshot_includes_state_and_model(self) -> None:
        service = _make_service()
        snapshot = service.snapshot()
        self.assertIn("state", snapshot)
        self.assertIn("model", snapshot)
        self.assertIn("preflight_results", snapshot)
        self.assertEqual(snapshot["state"], "idle")


if __name__ == "__main__":
    unittest.main()

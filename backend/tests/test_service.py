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


if __name__ == "__main__":
    unittest.main()

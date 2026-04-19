from __future__ import annotations

from pathlib import Path
import sys
import unittest

import httpx


ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from groq_whisper_service.client_pool import (  # noqa: E402
    RoundRobinTranscriptionClientPool,
    is_retryable_transcription_error,
    normalize_api_keys,
)


class FakeRetryableError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"retryable {status_code}")
        self.status_code = status_code


class FakeNonRetryableError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"non-retryable {status_code}")
        self.status_code = status_code


class FakeTranscriptions:
    def __init__(
        self,
        api_key: str,
        outcomes: list[object],
        call_log: list[str],
    ) -> None:
        self._api_key = api_key
        self._outcomes = outcomes
        self._call_log = call_log

    def create(self, **request: object) -> object:
        self._call_log.append(self._api_key)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return {"api_key": self._api_key, "request": request, "result": outcome}


class FakeAudio:
    def __init__(
        self,
        api_key: str,
        outcomes: list[object],
        call_log: list[str],
    ) -> None:
        self.transcriptions = FakeTranscriptions(api_key, outcomes, call_log)


class FakeClient:
    def __init__(
        self,
        api_key: str,
        outcomes: list[object],
        call_log: list[str],
    ) -> None:
        self.audio = FakeAudio(api_key, outcomes, call_log)


class ClientPoolTests(unittest.TestCase):
    def test_normalize_api_keys_trims_and_deduplicates(self) -> None:
        self.assertEqual(
            normalize_api_keys(["  A  ", "", "A", "B"]),
            ["A", "B"],
        )

    def test_round_robin_cycles_through_all_keys(self) -> None:
        call_log: list[str] = []
        outcome_map = {
            "A": ["ok-1", "ok-4"],
            "B": ["ok-2", "ok-5"],
            "C": ["ok-3", "ok-6"],
        }
        pool = RoundRobinTranscriptionClientPool(
            ["A", "B", "C"],
            client_factory=lambda api_key: FakeClient(api_key, outcome_map[api_key], call_log),
        )

        results = [pool.audio.transcriptions.create(request_id=index) for index in range(6)]

        self.assertEqual(call_log, ["A", "B", "C", "A", "B", "C"])
        self.assertEqual([result["api_key"] for result in results], ["A", "B", "C", "A", "B", "C"])
        self.assertEqual(pool.next_api_key, "A")

    def test_retryable_failure_uses_next_key_and_advances_cursor(self) -> None:
        call_log: list[str] = []
        outcome_map = {
            "A": [FakeRetryableError(429)],
            "B": ["recovered"],
            "C": ["next-request"],
        }
        pool = RoundRobinTranscriptionClientPool(
            ["A", "B", "C"],
            client_factory=lambda api_key: FakeClient(api_key, outcome_map[api_key], call_log),
        )

        first = pool.audio.transcriptions.create(request_id=1)
        second = pool.audio.transcriptions.create(request_id=2)

        self.assertEqual(call_log, ["A", "B", "C"])
        self.assertEqual(first["api_key"], "B")
        self.assertEqual(second["api_key"], "C")
        self.assertEqual(pool.next_api_key, "A")

    def test_single_distinct_key_does_not_retry_same_key(self) -> None:
        call_log: list[str] = []
        outcome_map = {
            "A": [FakeRetryableError(429)],
        }
        pool = RoundRobinTranscriptionClientPool(
            ["A", "A"],
            client_factory=lambda api_key: FakeClient(api_key, outcome_map[api_key], call_log),
        )

        with self.assertRaises(FakeRetryableError):
            pool.audio.transcriptions.create(request_id=1)

        self.assertEqual(call_log, ["A"])

    def test_non_retryable_failure_does_not_fail_over(self) -> None:
        call_log: list[str] = []
        outcome_map = {
            "A": [FakeNonRetryableError(401)],
            "B": ["should-not-run"],
        }
        pool = RoundRobinTranscriptionClientPool(
            ["A", "B"],
            client_factory=lambda api_key: FakeClient(api_key, outcome_map[api_key], call_log),
        )

        with self.assertRaises(FakeNonRetryableError):
            pool.audio.transcriptions.create(request_id=1)

        self.assertEqual(call_log, ["A"])

    def test_retryable_error_predicate_matches_sdk_and_status_codes(self) -> None:
        import groq

        request = httpx.Request("POST", "https://api.groq.com/openai/v1/audio/transcriptions")
        response = httpx.Response(status_code=429, request=request)

        self.assertTrue(is_retryable_transcription_error(FakeRetryableError(429)))
        self.assertFalse(is_retryable_transcription_error(FakeNonRetryableError(401)))
        self.assertTrue(
            is_retryable_transcription_error(
                groq.RateLimitError("rate limited", response=response, body=None),
            )
        )


if __name__ == "__main__":
    unittest.main()

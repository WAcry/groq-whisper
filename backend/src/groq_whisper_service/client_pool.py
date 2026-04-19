from __future__ import annotations

import threading
from typing import Any, Callable, Iterable

try:
    import groq
except ImportError:  # pragma: no cover - the package is installed in normal runtime paths.
    groq = None


RETRYABLE_STATUS_CODES = frozenset({408, 409, 429, 500, 502, 503, 504})


def normalize_api_keys(api_keys: Iterable[str] | None) -> list[str]:
    if api_keys is None:
        raise ValueError("Missing API keys. Save at least one key in Settings and try again.")

    normalized_keys: list[str] = []
    seen: set[str] = set()

    for api_key in api_keys:
        if not isinstance(api_key, str):
            raise ValueError("API keys must be strings.")
        normalized = api_key.strip()
        if not normalized:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        normalized_keys.append(normalized)

    if not normalized_keys:
        raise ValueError("Missing API keys. Save at least one key in Settings and try again.")

    return normalized_keys


def is_retryable_transcription_error(exc: Exception) -> bool:
    if groq is not None:
        if isinstance(exc, (groq.APIConnectionError, groq.APITimeoutError)):
            return True
        if isinstance(exc, (groq.RateLimitError, groq.InternalServerError)):
            return True
        if isinstance(exc, groq.APIStatusError):
            status_code = getattr(exc, "status_code", None)
            if isinstance(status_code, int):
                return status_code in RETRYABLE_STATUS_CODES

    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code in RETRYABLE_STATUS_CODES

    response = getattr(exc, "response", None)
    response_status_code = getattr(response, "status_code", None)
    if isinstance(response_status_code, int):
        return response_status_code in RETRYABLE_STATUS_CODES

    return False


class _TranscriptionsProxy:
    def __init__(self, pool: "RoundRobinTranscriptionClientPool") -> None:
        self._pool = pool

    def create(self, **request: Any) -> Any:
        return self._pool.create_transcription(**request)


class _AudioProxy:
    def __init__(self, pool: "RoundRobinTranscriptionClientPool") -> None:
        self.transcriptions = _TranscriptionsProxy(pool)


class RoundRobinTranscriptionClientPool:
    def __init__(
        self,
        api_keys: Iterable[str],
        *,
        client_factory: Callable[[str], Any],
        retryable_error_predicate: Callable[[Exception], bool] = is_retryable_transcription_error,
    ) -> None:
        self._api_keys = normalize_api_keys(api_keys)
        self._clients = [client_factory(api_key) for api_key in self._api_keys]
        self._retryable_error_predicate = retryable_error_predicate
        self._next_index = 0
        self._lock = threading.Lock()
        self.audio = _AudioProxy(self)

    @property
    def api_keys(self) -> tuple[str, ...]:
        return tuple(self._api_keys)

    @property
    def next_api_key(self) -> str:
        with self._lock:
            return self._api_keys[self._next_index]

    def create_transcription(self, **request: Any) -> Any:
        with self._lock:
            primary_index = self._next_index
            try:
                response = self._clients[primary_index].audio.transcriptions.create(**request)
            except Exception as exc:
                if len(self._clients) < 2 or not self._retryable_error_predicate(exc):
                    raise

                retry_index = self._advance_index(primary_index)
                response = self._clients[retry_index].audio.transcriptions.create(**request)
                self._next_index = self._advance_index(retry_index)
                return response

            self._next_index = self._advance_index(primary_index)
            return response

    def _advance_index(self, index: int) -> int:
        return (index + 1) % len(self._clients)

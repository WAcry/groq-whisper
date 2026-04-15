from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import threading
import uuid
from typing import Any


DEFAULT_DB_DIR = Path.home() / ".groq-whisper"
DEFAULT_DB_PATH = DEFAULT_DB_DIR / "sessions.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    model TEXT NOT NULL,
    language TEXT,
    prompt TEXT,
    full_text TEXT NOT NULL DEFAULT '',
    error_log TEXT,
    duration_seconds REAL,
    tick_count INTEGER NOT NULL DEFAULT 0,
    export_path TEXT
);
"""


class SessionStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or DEFAULT_DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def create_session(
        self,
        *,
        model: str,
        language: str | None = None,
        prompt: str | None = None,
    ) -> str:
        session_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (id, started_at, model, language, prompt) VALUES (?, ?, ?, ?, ?)",
                (session_id, now, model, language, prompt),
            )
            self._conn.commit()
        return session_id

    def update_text(
        self,
        session_id: str,
        *,
        full_text: str,
        tick_count: int,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET full_text = ?, tick_count = ? WHERE id = ?",
                (full_text, tick_count, session_id),
            )
            self._conn.commit()

    def finalize_session(
        self,
        session_id: str,
        *,
        full_text: str | None = None,
        error_log: str | None = None,
        duration_seconds: float | None = None,
        tick_count: int | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        parts = ["ended_at = ?"]
        params: list[Any] = [now]
        if full_text is not None:
            parts.append("full_text = ?")
            params.append(full_text)
        if error_log is not None:
            parts.append("error_log = ?")
            params.append(error_log)
        if duration_seconds is not None:
            parts.append("duration_seconds = ?")
            params.append(duration_seconds)
        if tick_count is not None:
            parts.append("tick_count = ?")
            params.append(tick_count)
        params.append(session_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE sessions SET {', '.join(parts)} WHERE id = ?",
                params,
            )
            self._conn.commit()

    def update_export_path(self, session_id: str, export_path: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET export_path = ? WHERE id = ?",
                (export_path, session_id),
            )
            self._conn.commit()

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    def list_sessions(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, started_at, ended_at, model, language, duration_seconds, tick_count, "
                "substr(full_text, 1, 200) AS text_preview "
                "FROM sessions ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_session(self, session_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM sessions WHERE id = ?",
                (session_id,),
            )
            self._conn.commit()
        return cursor.rowcount > 0

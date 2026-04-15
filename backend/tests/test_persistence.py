from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from groq_whisper_service.persistence import SessionStore


class SessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = SessionStore(db_path=Path(self.tmp.name))

    def tearDown(self) -> None:
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_create_and_get_session(self) -> None:
        sid = self.store.create_session(model="whisper-large-v3-turbo", language="en")
        session = self.store.get_session(sid)
        self.assertIsNotNone(session)
        self.assertEqual(session["model"], "whisper-large-v3-turbo")
        self.assertEqual(session["language"], "en")
        self.assertEqual(session["full_text"], "")
        self.assertIsNone(session["ended_at"])

    def test_update_text(self) -> None:
        sid = self.store.create_session(model="test")
        self.store.update_text(sid, full_text="hello world", tick_count=5)
        session = self.store.get_session(sid)
        self.assertEqual(session["full_text"], "hello world")
        self.assertEqual(session["tick_count"], 5)

    def test_finalize_session(self) -> None:
        sid = self.store.create_session(model="test")
        self.store.finalize_session(
            sid,
            full_text="final text",
            duration_seconds=42.5,
            tick_count=10,
        )
        session = self.store.get_session(sid)
        self.assertIsNotNone(session["ended_at"])
        self.assertEqual(session["full_text"], "final text")
        self.assertAlmostEqual(session["duration_seconds"], 42.5)
        self.assertEqual(session["tick_count"], 10)

    def test_finalize_with_error(self) -> None:
        sid = self.store.create_session(model="test")
        self.store.finalize_session(sid, error_log="something broke")
        session = self.store.get_session(sid)
        self.assertEqual(session["error_log"], "something broke")

    def test_list_sessions_ordered_newest_first(self) -> None:
        sid1 = self.store.create_session(model="m1")
        sid2 = self.store.create_session(model="m2")
        sessions = self.store.list_sessions()
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sessions[0]["id"], sid2)
        self.assertEqual(sessions[1]["id"], sid1)

    def test_list_sessions_pagination(self) -> None:
        for i in range(5):
            self.store.create_session(model=f"m{i}")
        page1 = self.store.list_sessions(limit=2, offset=0)
        page2 = self.store.list_sessions(limit=2, offset=2)
        self.assertEqual(len(page1), 2)
        self.assertEqual(len(page2), 2)
        self.assertNotEqual(page1[0]["id"], page2[0]["id"])

    def test_list_sessions_includes_text_preview(self) -> None:
        sid = self.store.create_session(model="test")
        self.store.update_text(sid, full_text="a" * 500, tick_count=1)
        sessions = self.store.list_sessions()
        self.assertEqual(len(sessions[0]["text_preview"]), 200)

    def test_delete_session(self) -> None:
        sid = self.store.create_session(model="test")
        self.assertTrue(self.store.delete_session(sid))
        self.assertIsNone(self.store.get_session(sid))

    def test_delete_nonexistent_returns_false(self) -> None:
        self.assertFalse(self.store.delete_session("nonexistent"))

    def test_get_nonexistent_returns_none(self) -> None:
        self.assertIsNone(self.store.get_session("nonexistent"))

    def test_update_export_path(self) -> None:
        sid = self.store.create_session(model="test")
        self.store.update_export_path(sid, "/path/to/export.txt")
        session = self.store.get_session(sid)
        self.assertEqual(session["export_path"], "/path/to/export.txt")


if __name__ == "__main__":
    unittest.main()

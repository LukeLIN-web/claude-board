"""Codex goals are native thread state, independent of rollout writes."""
import sqlite3
import unittest
from contextlib import closing
from unittest import mock

from core import codex, transcripts
from core.textcap import GOAL_CHARS
from tests.helpers import make_window, scratch_dir, write_jsonl


class TestCodexGoal(unittest.TestCase):
    def setUp(self):
        self.home = scratch_dir() / "codex"
        self.db = self.home / "goals_1.sqlite"
        patch = mock.patch.object(codex, "CODEX_HOME", self.home)
        patch.start()
        self.addCleanup(patch.stop)

    def _create_db(self):
        self.home.mkdir()
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("""
                CREATE TABLE thread_goals (
                    thread_id TEXT PRIMARY KEY NOT NULL,
                    goal_id TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    status TEXT NOT NULL,
                    token_budget INTEGER,
                    tokens_used INTEGER NOT NULL DEFAULT 0,
                    time_used_seconds INTEGER NOT NULL DEFAULT 0,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                )
            """)

    def _set_goal(self, sid, text, *, updated=1_792_584_000_000, status="active"):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO thread_goals
                    (thread_id, goal_id, objective, status, created_at_ms, updated_at_ms)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (sid, "goal-" + sid, text, status, updated, updated))
            conn.commit()

    def test_missing_database_does_not_create_files_or_directory(self):
        self.assertIsNone(codex.session_goal("thread-a"))
        self.assertFalse(self.home.exists())
        self.home.mkdir()
        self.assertIsNone(codex.session_goal("thread-a"))
        self.assertEqual(list(self.home.iterdir()), [])

    def test_only_exact_thread_goal_is_returned(self):
        self._create_db()
        self._set_goal("thread-a", "Finish the board. Keep the second sentence.")
        self._set_goal("thread-ab", "A different goal")
        goal = codex.session_goal("thread-a")
        self.assertEqual(goal["text"], "Finish the board. Keep the second sentence.")
        self.assertEqual(goal["source"], "codex")
        self.assertEqual(transcripts._parse_ts(goal["ts"]), 1_792_584_000)
        for sid in ("thread", "thread-a' OR 1=1 --", "", "codex-9999"):
            with self.subTest(sid=sid):
                self.assertIsNone(codex.session_goal(sid))

    def test_empty_objective_is_not_a_goal(self):
        self._create_db()
        for objective in ("", " \n\t"):
            with self.subTest(objective=objective):
                self._set_goal("thread-a", objective)
                self.assertIsNone(codex.session_goal("thread-a"))

    def test_goal_uses_existing_text_cap(self):
        self._create_db()
        self._set_goal("thread-a", "x" * (GOAL_CHARS + 100))
        text = codex.session_goal("thread-a")["text"]
        self.assertTrue(text.startswith("x" * GOAL_CHARS))
        self.assertLess(len(text), GOAL_CHARS + 100)
        self.assertIn("100", text)

    def test_complete_goal_keeps_objective_without_status_suffix(self):
        self._create_db()
        self._set_goal("thread-a", "Ship the board", status="complete")
        self.assertEqual(codex.session_goal("thread-a")["text"], "Ship the board")

    def test_unreadable_schema_or_database_returns_no_goal(self):
        self.home.mkdir()
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("CREATE TABLE thread_goals (thread_id TEXT, objective TEXT)")
        self.assertIsNone(codex.session_goal("thread-a"))
        self.db.write_bytes(b"not a sqlite database")
        self.assertIsNone(codex.session_goal("thread-a"))

    def test_locked_database_returns_no_goal(self):
        self._create_db()
        self._set_goal("thread-a", "Ship the board")
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("BEGIN EXCLUSIVE")
            self.assertIsNone(codex.session_goal("thread-a"))

    def test_live_timeline_reads_goal_updates_without_rollout_changes(self):
        self._create_db()
        self._set_goal("thread-a", "First goal")
        rollout = write_jsonl([{
            "type": "event_msg", "timestamp": "2026-10-21T12:00:00Z",
            "payload": {"type": "user_message", "message": "Continue"},
        }])
        window = make_window(platform="codex", session_id="thread-a", transcript_path=str(rollout))
        initial_mtime = rollout.stat().st_mtime_ns
        first = codex.live_timeline(window, 60)
        self.assertEqual(first["goal"]["text"], "First goal")
        self._set_goal("thread-a", "Updated goal", updated=1_792_584_001_000)
        updated = codex.live_timeline(window, 60)
        self.assertEqual(updated["goal"]["text"], "Updated goal")
        self.assertNotEqual(updated["goal"]["ts"], first["goal"]["ts"])
        self.assertEqual(updated["events"], first["events"])
        self.assertEqual(rollout.stat().st_mtime_ns, initial_mtime)

    def test_live_timeline_can_show_goal_without_a_rollout(self):
        self._create_db()
        self._set_goal("thread-a", "Ship the board")
        window = make_window(platform="codex", session_id="thread-a")
        timeline = codex.live_timeline(window, 60)
        self.assertEqual(timeline["events"], [])
        self.assertEqual(timeline["goal"]["text"], "Ship the board")

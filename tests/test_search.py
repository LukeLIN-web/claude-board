"""Tests for transcript search (core/search.py) and the History panel's full-text
match, which runs the same rg command (core/history.py) and places snippets the
same way (core/history.py, core/opencode.py).

The searches run the real ripgrep over transcripts written to a scratch home, so
those tests skip where `rg` isn't on PATH.
"""
import json
import os
import shutil
import sqlite3
import unittest
from unittest import mock

from core import history, opencode, search, sessions
from tests.helpers import scratch_dir, user_row, write_jsonl


@unittest.skipUnless(shutil.which("rg"), "ripgrep (rg) is not on PATH")
class _Transcripts(unittest.TestCase):
    """A scratch projects/ and Codex sessions/ for search to run over, with the
    cwd filter off (run.sh exports one, and the board's sessions inherit it)."""

    def setUp(self):
        root = scratch_dir()
        self.projects = root / "projects"
        self.projects.mkdir()
        env = mock.patch.dict(os.environ, {"CLAUDE_FLEET_CWD_INCLUDE": "",
                                           "CLAUDE_FLEET_CWD_EXCLUDE": ""})
        env.start()
        # Cleanups run last-in first-out: the env goes back, then is re-read.
        self.addCleanup(sessions._reload_cwd_filters)
        self.addCleanup(env.stop)
        sessions._reload_cwd_filters()
        for p in (mock.patch.object(search, "PROJECTS_DIR", self.projects),
                  mock.patch.object(search, "CODEX_SESSIONS_DIR", root / "codex")):
            p.start()
            self.addCleanup(p.stop)

    def transcript(self, sid, *texts, ts="2026-08-10T21:26:21Z"):
        """A Claude transcript `sid` with one user row per text, all at `ts`."""
        return write_jsonl([user_row(t, ts=ts) for t in texts],
                           self.projects / "-tmp-proj" / f"{sid}.jsonl")


class LiteralQueryTests(_Transcripts):
    """The query is the text typed, not a regex and never an rg flag."""

    def test_regex_metacharacters_are_plain_text(self):
        self.transcript("s1", "then call foo(bar) twice")
        hits = search.search("foo(")
        self.assertEqual([h["line"] for h in hits], [1])
        self.assertIn("foo(bar)", hits[0]["excerpt"])

    def test_query_starting_with_a_dash_is_searched_for(self):
        self.transcript("s1", "pick it up with claude --resume later")
        hits = search.search("--resume")
        self.assertEqual([h["line"] for h in hits], [1])

    def test_dot_matches_only_a_dot(self):
        self.transcript("s1", "axb", "a.b")
        self.assertEqual([h["line"] for h in search.search("a.b")], [2])

    def test_smart_case(self):
        self.transcript("s1", "Edit the file", "edit the file")
        self.assertEqual(sorted(h["line"] for h in search.search("edit")), [1, 2])
        self.assertEqual([h["line"] for h in search.search("Edit")], [1])

    def test_excerpt_lands_on_the_match_rg_made(self):
        # rg matched "Edit" exactly; the excerpt has to show that one, not the
        # first "edit" forty words earlier.
        self.transcript("s1", "edit " * 40 + "Edit here")
        hits = search.search("Edit")
        self.assertEqual(len(hits), 1)
        self.assertIn("Edit here", hits[0]["excerpt"])

    def test_history_match_is_literal(self):
        self.transcript("s1", "then call foo(bar) twice")
        self.transcript("s2", "pick it up with claude --resume later")
        self.transcript("s3", "axb")
        self.assertEqual(list(history._rg_search_sessions("foo(")), ["s1"])
        self.assertEqual(list(history._rg_search_sessions("--resume")), ["s2"])
        self.assertEqual(history._rg_search_sessions("a.b"), {})


class ExcerptTests(unittest.TestCase):
    def test_follows_smart_case(self):
        text = "edit " * 40 + "Edit here"
        self.assertIn("Edit here", search.excerpt(text, "Edit"))
        self.assertTrue(search.excerpt(text, "edit").startswith("edit"))

    def test_query_is_plain_text(self):
        self.assertIsNone(search.find("axb", "a.b"))
        self.assertEqual(search.find("call foo(bar)", "foo(").start(), 5)


class OpenCodeSearchTests(unittest.TestCase):
    """OpenCode's parts are matched by the rule the transcript search uses."""

    def setUp(self):
        db = scratch_dir() / "opencode.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE part (session_id TEXT, data TEXT)")
        conn.executemany("INSERT INTO part VALUES (?, ?)", [
            ("lower", json.dumps({"type": "text", "text": "edit the file"})),
            ("upper", json.dumps({"type": "text", "text": "Edit the file"})),
        ])
        conn.commit()
        conn.close()
        p = mock.patch.object(opencode, "OPENCODE_DB", db)
        p.start()
        self.addCleanup(p.stop)

    def test_smart_case(self):
        self.assertEqual(sorted(opencode.search_opencode("edit")), ["lower", "upper"])
        self.assertEqual(list(opencode.search_opencode("Edit")), ["upper"])


if __name__ == "__main__":
    unittest.main()

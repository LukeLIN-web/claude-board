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
from datetime import datetime, timezone
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

    def transcript(self, sid, *texts, ts="2026-08-10T21:26:21Z", mtime=None, **row):
        """A Claude transcript `sid` with one user row per text, all at `ts`,
        last written at `mtime` if given. Extra keyword fields go on each row."""
        path = write_jsonl([user_row(t, ts=ts, **row) for t in texts],
                           self.projects / "-tmp-proj" / f"{sid}.jsonl")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path


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


T0 = 1_786_000_000  # an epoch second in August 2026


def iso(epoch):
    """`epoch` as a transcript row's timestamp."""
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class NewestHitsTests(_Transcripts):
    """Search returns the newest hits there are, not the first ones rg printed."""

    def written_at(self, sid, mtime, hits=5, rows_at=None, **row):
        """`sid` with `hits` rows mentioning "needle", written at `rows_at`
        (a second before `mtime` unless given), and last written at `mtime`."""
        ts = iso(mtime - 1 if rows_at is None else rows_at)
        return self.transcript(sid, *[f"needle {i}" for i in range(hits)],
                               ts=ts, mtime=mtime, **row)

    def sids(self, hits):
        return {h["session_id"] for h in hits}

    def test_newest_file_wins(self):
        self.written_at("old", T0)
        self.written_at("new", T0 + 3600)
        hits = search.search("needle", limit=5)
        self.assertEqual(self.sids(hits), {"new"})
        self.assertEqual(len(hits), 5)

    def test_newest_files_among_many(self):
        # The newest five sit mid-way through both the names and the order the
        # files were made in, so no walk order hands them over first.
        for i in range(30):
            self.written_at(f"s{i:02d}", T0 + i + (3600 if 12 <= i < 17 else 0), hits=1)
        hits = search.search("needle", limit=5)
        self.assertEqual(self.sids(hits), {f"s{i:02d}" for i in range(12, 17)})
        self.assertEqual([h["ts"] for h in hits], sorted((h["ts"] for h in hits), reverse=True))

    def test_newer_hit_in_an_older_file_still_makes_the_cut(self):
        # "touched" was written to last, but only its first rows mention the
        # query and they are a day old; "recent" was written an hour ago.
        path = self.written_at("touched", T0 + 7200, rows_at=T0 - 86400)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(user_row("something else", ts=iso(T0 + 7199))) + "\n")
        os.utime(path, (T0 + 7200, T0 + 7200))
        self.written_at("recent", T0 + 3600)
        self.assertEqual(self.sids(search.search("needle", limit=5)), {"recent"})

    def test_stops_once_older_files_cannot_make_the_cut(self):
        # No real row is newer than its file's last write; this one is, so a
        # search that went on to read "older" would put it first.
        self.written_at("newer", T0 + 3600)
        self.written_at("older", T0, rows_at=T0 + 7200)
        self.assertEqual(self.sids(search.search("needle", limit=5)), {"newer"})

    def test_at_most_five_hits_per_file(self):
        self.written_at("chatty", T0, hits=8)
        self.written_at("quiet", T0 - 60, hits=1)
        hits = search.search("needle", limit=40)
        self.assertEqual(sorted(h["session_id"] for h in hits), ["chatty"] * 5 + ["quiet"])

    def test_hidden_projects_do_not_use_up_the_limit(self):
        for i in range(3):
            self.written_at(f"hidden{i}", T0 + 3600 + i, cwd="/tmp/hidden/p")
        self.written_at("shown", T0, hits=2, cwd="/tmp/proj")
        with mock.patch.dict(os.environ, {"CLAUDE_FLEET_CWD_EXCLUDE": "/tmp/hidden"}):
            sessions._reload_cwd_filters()
            hits = search.search("needle", limit=2)
        sessions._reload_cwd_filters()
        self.assertEqual([h["session_id"] for h in hits], ["shown", "shown"])


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

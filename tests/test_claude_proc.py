"""Tests for process-first detection of live `claude` sessions that have not yet
written a ~/.claude/sessions/<pid>.json file (fresh spawns / resume picker)."""
import os
import unittest
from unittest import mock

from core import sessions


class ParseClaudeProcTests(unittest.TestCase):
    def test_bare_claude_is_interactive(self):
        self.assertEqual(sessions._parse_claude_proc("claude"), {"session_id": ""})

    def test_resume_id_is_parsed(self):
        self.assertEqual(
            sessions._parse_claude_proc("claude --resume f0eb279f-96d2"),
            {"session_id": "f0eb279f-96d2"},
        )

    def test_node_launcher_form(self):
        self.assertEqual(
            sessions._parse_claude_proc("node /n/bin/claude --resume abc"),
            {"session_id": "abc"},
        )

    def test_print_mode_is_not_a_window(self):
        self.assertIsNone(sessions._parse_claude_proc("claude -p 'do a thing'"))
        self.assertIsNone(sessions._parse_claude_proc("claude --print hello"))

    def test_headless_subcommands_excluded(self):
        self.assertIsNone(sessions._parse_claude_proc("claude mcp serve"))
        self.assertIsNone(sessions._parse_claude_proc("claude config set x y"))

    def test_non_claude_process(self):
        self.assertIsNone(sessions._parse_claude_proc("vim notes.md"))
        self.assertIsNone(sessions._parse_claude_proc("python claude_helper.py"))


@unittest.skipUnless(os.path.isdir("/proc"), "process-first detection is Linux-only")
class ListClaudeProcWindowsTests(unittest.TestCase):
    PS = (
        "212704 pts/3 claude --resume f0eb279f-96d2\n"
        "197794 pts/1 claude\n"            # already carded by its session file
        "55501 ? node /n/bin/claude mcp\n"  # headless, no tty
        "9001 pts/9 claude -p scripted\n"   # print mode → skip
        "777 pts/8 vim file\n"              # not claude
    )

    def _run(self, known_pids=frozenset(), known_ttys=frozenset()):
        # Relies on a real /proc (Linux); the transcript path resolves against
        # the real PROJECTS_DIR and simply won't exist, which is what we want.
        with mock.patch("core.sessions.subprocess.check_output",
                        return_value=self.PS.encode()), \
             mock.patch("core.sessions._pid_alive", return_value=True), \
             mock.patch("core.sessions._cwd_visible", return_value=True), \
             mock.patch("core.sessions.os.readlink", return_value="/work/qwen3-Omni"):
            return sessions.list_claude_proc_windows(set(known_pids), set(known_ttys))

    def test_only_interactive_unknown_claude_is_carded(self):
        wins = self._run()
        pids = {w.pid for w in wins}
        self.assertEqual(pids, {212704, 197794})  # the two interactive TUIs
        w = next(w for w in wins if w.pid == 212704)
        self.assertEqual(w.session_id, "f0eb279f-96d2")
        self.assertEqual(w.platform, "claude")
        self.assertEqual(w.tty, "/dev/pts/3")
        self.assertEqual(w.status, "waiting")  # seeded for pane verification

    def test_pid_already_known_is_skipped(self):
        wins = self._run(known_pids={212704})
        self.assertNotIn(212704, {w.pid for w in wins})

    def test_tty_already_known_is_skipped(self):
        wins = self._run(known_ttys={"/dev/pts/3"})
        self.assertNotIn(212704, {w.pid for w in wins})


class FindWindowProcFallbackTests(unittest.TestCase):
    """find_window / find_window_by_session must resolve process-first Claude
    cards too, or the card's actions (timeline, menu, prompt) 404."""

    def _fake(self, pid=999, sid="f0eb279f-aaaa"):
        return sessions.Window(
            pid=pid, session_id=sid, cwd="/w", project_name="w",
            project_slug="-w", name=None, status="waiting",
            waiting_for="dialog open", started_at=0, updated_at=0,
            version="", tty="/dev/pts/3", transcript_path=None,
            alive=True, hidden=False, platform="claude")

    def test_find_window_falls_back_to_proc(self):
        with mock.patch("core.sessions.list_windows", return_value=[]), \
             mock.patch("core.sessions.list_claude_proc_windows",
                        return_value=[self._fake(pid=999)]):
            w = sessions.find_window(999)
        self.assertIsNotNone(w)
        self.assertEqual(w.pid, 999)

    def test_find_window_by_session_falls_back_to_proc(self):
        with mock.patch("core.sessions.list_windows", return_value=[]), \
             mock.patch("core.sessions.list_claude_proc_windows",
                        return_value=[self._fake(sid="f0eb279f-aaaa")]):
            w = sessions.find_window_by_session("f0eb279f-aaaa")
        self.assertIsNotNone(w)
        self.assertEqual(w.session_id, "f0eb279f-aaaa")


class DiscoverProcTranscriptTests(unittest.TestCase):
    """A fresh `claude` spawn (no --resume id, no session file) must still link
    to the transcript it writes, so the card and prompt queue aren't blank."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.projects = Path(self._tmp.name)
        self.slug = "-work-qwen3-Omni"
        (self.projects / self.slug).mkdir()
        self._patch = mock.patch("core.sessions.PROJECTS_DIR", self.projects)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(self._tmp.cleanup)

    def _touch(self, sid, mtime_s):
        f = self.projects / self.slug / f"{sid}.jsonl"
        f.write_text('{"type":"summary","sessionId":"%s"}\n' % sid)
        os.utime(f, (mtime_s, mtime_s))
        return f

    def test_picks_newest_transcript_after_process_start(self):
        self._touch("old-session", mtime_s=1000)
        self._touch("new-session", mtime_s=5000)
        sid, path = sessions._discover_proc_transcript(
            self.slug, start_ms=4000 * 1000, claimed_sids=set())
        self.assertEqual(sid, "new-session")
        self.assertTrue(path.endswith("new-session.jsonl"))

    def test_skips_transcripts_older_than_process_start(self):
        # Predates start by more than the 2-min grace -> a prior session.
        self._touch("stale", mtime_s=1000)
        sid, path = sessions._discover_proc_transcript(
            self.slug, start_ms=5000 * 1000, claimed_sids=set())
        self.assertIsNone(sid)
        self.assertIsNone(path)

    def test_skips_already_claimed_sid(self):
        self._touch("taken", mtime_s=5000)
        sid, _ = sessions._discover_proc_transcript(
            self.slug, start_ms=4000 * 1000, claimed_sids={"taken"})
        self.assertIsNone(sid)

    def test_no_match_returns_none(self):
        sid, path = sessions._discover_proc_transcript(
            self.slug, start_ms=1000, claimed_sids=set())
        self.assertIsNone(sid)
        self.assertIsNone(path)


@unittest.skipUnless(os.path.isdir("/proc"), "process-first detection is Linux-only")
class ResumeForkTests(unittest.TestCase):
    """`--resume <oldid>` forks a new id in recent Claude; the card must follow
    the forked (live) transcript, not the frozen resume-arg file."""

    def setUp(self):
        import tempfile
        import time
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.projects = Path(self._tmp.name)
        self.cwd = "/work/qwen3-Omni"
        self.slug = "-work-qwen3-Omni"
        (self.projects / self.slug).mkdir()
        self.addCleanup(self._tmp.cleanup)
        p = mock.patch("core.sessions.PROJECTS_DIR", self.projects)
        p.start()
        self.addCleanup(p.stop)
        self.now = time.time()

    def _touch(self, sid, mtime):
        f = self.projects / self.slug / f"{sid}.jsonl"
        f.write_text('{"type":"summary","sessionId":"%s"}\n' % sid)
        os.utime(f, (mtime, mtime))

    def _run(self, ps):
        with mock.patch("core.sessions.subprocess.check_output", return_value=ps.encode()), \
             mock.patch("core.sessions._pid_alive", return_value=True), \
             mock.patch("core.sessions._cwd_visible", return_value=True), \
             mock.patch("core.sessions.os.readlink", return_value=self.cwd):
            return sessions.list_claude_proc_windows(set(), set())

    def test_adopts_forked_transcript_over_frozen_resume_arg(self):
        self._touch("oldid", mtime=self.now - 3600)   # frozen at pre-resume point
        self._touch("newforkid", mtime=self.now)      # live forked continuation
        pid = os.getpid()
        w = next(w for w in self._run(f"{pid} pts/3 claude --resume oldid\n")
                 if w.pid == pid)
        self.assertEqual(w.session_id, "newforkid")
        self.assertTrue(w.transcript_path.endswith("newforkid.jsonl"))

    def test_keeps_resume_arg_when_no_newer_sibling(self):
        # Older-Claude behavior: the session appends to the resume-arg file, so
        # there's no newer sibling and the card keeps the resume-arg id.
        self._touch("oldid", mtime=self.now)
        pid = os.getpid()
        w = next(w for w in self._run(f"{pid} pts/3 claude --resume oldid\n")
                 if w.pid == pid)
        self.assertEqual(w.session_id, "oldid")


if __name__ == "__main__":
    unittest.main()

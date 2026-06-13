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


if __name__ == "__main__":
    unittest.main()

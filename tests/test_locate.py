"""Tests for session-id → tmux pane reverse lookup (/api/locate + helpers)."""
import unittest
from unittest import mock

import app as appmod
from core import sessions, tmux


def _win(session_id: str, pid: int = 100, tty: str = "/dev/pts/3") -> sessions.Window:
    return sessions.Window(
        pid=pid, session_id=session_id, cwd="/tmp/proj", project_name="proj",
        project_slug="-tmp-proj", name=None, status="busy", waiting_for=None,
        started_at=0, updated_at=0, version="2.1.0", tty=tty,
        transcript_path=None, alive=True, hidden=False,
    )


class FindWindowBySessionTests(unittest.TestCase):
    def _patch(self, windows, codex_windows=()):
        return mock.patch.multiple(
            sessions,
            list_windows=mock.Mock(return_value=list(windows)),
        ), mock.patch(
            "core.codex.list_codex_windows", return_value=list(codex_windows),
        )

    def test_exact_match(self):
        w = _win("8ce5b822-e854-4608-a668-a726e26e9256")
        p1, p2 = self._patch([w])
        with p1, p2:
            got = sessions.find_window_by_session("8CE5B822-E854-4608-A668-A726E26E9256")
        self.assertIs(got, w)

    def test_unique_prefix_match(self):
        w1, w2 = _win("8ce5b822-aaaa"), _win("27996304-bbbb", pid=101)
        p1, p2 = self._patch([w1, w2])
        with p1, p2:
            self.assertIs(sessions.find_window_by_session("8ce5b822"), w1)

    def test_short_prefix_rejected(self):
        w = _win("8ce5b822-aaaa")
        p1, p2 = self._patch([w])
        with p1, p2:
            self.assertIsNone(sessions.find_window_by_session("8ce5"))

    def test_ambiguous_prefix_returns_none(self):
        w1, w2 = _win("8ce5b822-aaaa"), _win("8ce5b822-bbbb", pid=101)
        p1, p2 = self._patch([w1, w2])
        with p1, p2:
            self.assertIsNone(sessions.find_window_by_session("8ce5b822"))

    def test_codex_windows_searched_too(self):
        cw = _win("0199c00c-codex", pid=200)
        p1, p2 = self._patch([], codex_windows=[cw])
        with p1, p2:
            self.assertIs(sessions.find_window_by_session("0199c00c"), cw)

    def test_empty_id_returns_none(self):
        p1, p2 = self._patch([_win("8ce5b822-aaaa")])
        with p1, p2:
            self.assertIsNone(sessions.find_window_by_session(""))


class LocateRouteTests(unittest.TestCase):
    def test_locate_resolves_pane_and_target(self):
        w = _win("8ce5b822-aaaa")
        with mock.patch.object(appmod.sessions, "find_window_by_session", return_value=w), \
             mock.patch.object(appmod.tmux, "pane_for_tty", return_value="%3") as pft, \
             mock.patch.object(appmod.tmux, "pane_target", return_value="j1:2.0"):
            r = appmod.api_locate("8ce5b822")
        pft.assert_called_once_with("/dev/pts/3")
        self.assertEqual(r["tmux_pane"], "%3")
        self.assertEqual(r["tmux_target"], "j1:2.0")
        self.assertEqual(r["window"]["session_id"], "8ce5b822-aaaa")

    def test_locate_404_when_unknown(self):
        import fastapi
        with mock.patch.object(appmod.sessions, "find_window_by_session", return_value=None):
            with self.assertRaises(fastapi.HTTPException):
                appmod.api_locate("deadbeef")

    def test_locate_without_tty_returns_null_pane(self):
        w = _win("8ce5b822-aaaa", tty=None)
        with mock.patch.object(appmod.sessions, "find_window_by_session", return_value=w), \
             mock.patch.object(appmod.tmux, "pane_for_tty") as pft:
            r = appmod.api_locate("8ce5b822")
        pft.assert_not_called()
        self.assertIsNone(r["tmux_pane"])
        self.assertIsNone(r["tmux_target"])


class PaneTargetTests(unittest.TestCase):
    def test_pane_target_formats_query(self):
        with mock.patch.object(tmux, "_run", return_value={"ok": True, "stdout": "j1:2.0\n"}) as m:
            self.assertEqual(tmux.pane_target("%3"), "j1:2.0")
        self.assertIn("%3", m.call_args[0])

    def test_pane_target_none_on_failure(self):
        with mock.patch.object(tmux, "_run", return_value={"ok": False, "stdout": "", "error": "x"}):
            self.assertIsNone(tmux.pane_target("%3"))
        self.assertIsNone(tmux.pane_target(""))


if __name__ == "__main__":
    unittest.main()

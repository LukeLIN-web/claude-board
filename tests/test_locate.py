"""Tests for session-id → tmux pane reverse lookup (/api/locate + helpers)."""
import contextlib
import unittest
from unittest import mock

import app as appmod
from core import sessions, tmux
from tests.helpers import make_window


def _win(session_id: str, **over) -> sessions.Window:
    return make_window(session_id=session_id, **over)


@contextlib.contextmanager
def _windows(claude=(), codex=(), hmz=()):
    """Every source a lookup searches, each holding exactly these windows.

    The process-backed ones (fresh Claude spawns, Codex, hmz) would otherwise
    read this machine's real process table."""
    with mock.patch.object(sessions, "list_windows", return_value=list(claude)), \
            mock.patch.object(sessions, "list_claude_proc_windows", return_value=[]), \
            mock.patch("core.codex.list_codex_windows", return_value=list(codex)), \
            mock.patch("core.hmz.list_hmz_windows", return_value=list(hmz)):
        yield


class FindWindowBySessionTests(unittest.TestCase):
    def test_exact_match(self):
        w = _win("8ce5b822-e854-4608-a668-a726e26e9256")
        with _windows([w]):
            got = sessions.find_window_by_session("8CE5B822-E854-4608-A668-A726E26E9256")
        self.assertIs(got, w)

    def test_unique_prefix_match(self):
        w1, w2 = _win("8ce5b822-aaaa"), _win("27996304-bbbb", pid=101)
        with _windows([w1, w2]):
            self.assertIs(sessions.find_window_by_session("8ce5b822"), w1)

    def test_short_prefix_rejected(self):
        with _windows([_win("8ce5b822-aaaa")]):
            self.assertIsNone(sessions.find_window_by_session("8ce5"))

    def test_ambiguous_prefix_returns_none(self):
        w1, w2 = _win("8ce5b822-aaaa"), _win("8ce5b822-bbbb", pid=101)
        with _windows([w1, w2]):
            self.assertIsNone(sessions.find_window_by_session("8ce5b822"))

    def test_codex_windows_searched_too(self):
        cw = _win("0199c00c-codex", pid=200)
        with _windows(codex=[cw]):
            self.assertIs(sessions.find_window_by_session("0199c00c"), cw)

    def test_hmz_windows_searched_too(self):
        hw = _win("20261002T005030.513Z-a779c6", pid=300)
        with _windows(hmz=[hw]):
            self.assertIs(sessions.find_window_by_session("20261002T005030"), hw)

    def test_empty_id_returns_none(self):
        with _windows([_win("8ce5b822-aaaa")]):
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

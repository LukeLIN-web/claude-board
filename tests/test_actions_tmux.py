"""Tests for the tmux-backed wrappers in core/actions.py (create_session, send_prompt)."""
import os
import tempfile
import types
import unittest
from unittest import mock

from core import actions


def _fake_window(tty):
    return types.SimpleNamespace(tty=tty)


class CreateSessionTests(unittest.TestCase):
    def test_existing_dir_delegates_to_new_window(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(actions.tmux, "new_window", return_value={"ok": True, "pane_id": "%1"}) as m:
                r = actions.create_session(d)
            m.assert_called_once_with(d)
            self.assertTrue(r["ok"])

    def test_tilde_is_expanded_before_validation(self):
        captured = {}

        def fake_new_window(cwd):
            captured["cwd"] = cwd
            return {"ok": True, "pane_id": "%2"}

        with mock.patch.object(actions.tmux, "new_window", side_effect=fake_new_window):
            r = actions.create_session("~")
        self.assertTrue(r["ok"])
        self.assertEqual(captured["cwd"], os.path.expanduser("~"))

    def test_empty_cwd_rejected_without_touching_tmux(self):
        with mock.patch.object(actions.tmux, "new_window") as m:
            r = actions.create_session("")
        self.assertFalse(r["ok"])
        self.assertTrue(r["error"])
        m.assert_not_called()

    def test_nonexistent_cwd_rejected_without_touching_tmux(self):
        with mock.patch.object(actions.tmux, "new_window") as m:
            r = actions.create_session("/no/such/dir/really/xyz")
        self.assertFalse(r["ok"])
        m.assert_not_called()

    def test_file_path_rejected(self):
        with tempfile.NamedTemporaryFile() as f:
            with mock.patch.object(actions.tmux, "new_window") as m:
                r = actions.create_session(f.name)
        self.assertFalse(r["ok"])
        m.assert_not_called()


class SendPromptTests(unittest.TestCase):
    def test_happy_path_resolves_pane_and_sends(self):
        with mock.patch.object(actions, "find_window", return_value=_fake_window("/dev/pts/3")), \
             mock.patch.object(actions.tmux, "pane_for_tty", return_value="%5") as pf, \
             mock.patch.object(actions.tmux, "send_text", return_value={"ok": True}) as st:
            r = actions.send_prompt(1234, "hello")
        pf.assert_called_once_with("/dev/pts/3")
        st.assert_called_once_with("%5", "hello")
        self.assertTrue(r["ok"])

    def test_newlines_collapsed_to_spaces(self):
        with mock.patch.object(actions, "find_window", return_value=_fake_window("/dev/pts/3")), \
             mock.patch.object(actions.tmux, "pane_for_tty", return_value="%5"), \
             mock.patch.object(actions.tmux, "send_text", return_value={"ok": True}) as st:
            actions.send_prompt(1234, "line1\nline2\nline3")
        self.assertEqual(st.call_args[0][1], "line1 line2 line3")

    def test_no_pane_returns_explicit_error(self):
        with mock.patch.object(actions, "find_window", return_value=_fake_window("/dev/pts/3")), \
             mock.patch.object(actions.tmux, "pane_for_tty", return_value=None), \
             mock.patch.object(actions.tmux, "send_text") as st:
            r = actions.send_prompt(1234, "hello")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "session not in a tmux pane")
        st.assert_not_called()

    def test_missing_window_returns_error(self):
        with mock.patch.object(actions, "find_window", return_value=None), \
             mock.patch.object(actions.tmux, "send_text") as st:
            r = actions.send_prompt(1234, "hello")
        self.assertFalse(r["ok"])
        st.assert_not_called()

    def test_empty_text_rejected_before_send(self):
        with mock.patch.object(actions, "find_window", return_value=_fake_window("/dev/pts/3")), \
             mock.patch.object(actions.tmux, "pane_for_tty", return_value="%5"), \
             mock.patch.object(actions.tmux, "send_text") as st:
            r = actions.send_prompt(1234, "   \n  ")
        self.assertFalse(r["ok"])
        st.assert_not_called()

    def test_oversized_text_rejected_before_send(self):
        big = "a" * 8001
        with mock.patch.object(actions, "find_window", return_value=_fake_window("/dev/pts/3")), \
             mock.patch.object(actions.tmux, "pane_for_tty", return_value="%5"), \
             mock.patch.object(actions.tmux, "send_text") as st:
            r = actions.send_prompt(1234, big)
        self.assertFalse(r["ok"])
        self.assertIn("8000", r["error"])
        st.assert_not_called()

    def test_max_length_accepted(self):
        ok_text = "a" * 8000
        with mock.patch.object(actions, "find_window", return_value=_fake_window("/dev/pts/3")), \
             mock.patch.object(actions.tmux, "pane_for_tty", return_value="%5"), \
             mock.patch.object(actions.tmux, "send_text", return_value={"ok": True}) as st:
            r = actions.send_prompt(1234, ok_text)
        self.assertTrue(r["ok"])
        st.assert_called_once()


if __name__ == "__main__":
    unittest.main()

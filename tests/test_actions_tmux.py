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


class ParsePaneMenuTests(unittest.TestCase):
    """parse_pane_menu reads the live picker/permission menu off a captured pane."""

    def test_single_question_picker(self):
        cap = (
            " \u2610 \u6d4b\u8bd5\n\u6d4b\u8bd5\u95ee\u9898\n"
            "\u276f 1. A\n  2. B\n  3. C\n  4. Type something.\n"
            "  5. Chat about this\n"
            "Enter to select \u00b7 \u2191/\u2193 to navigate \u00b7 Esc to cancel"
        )
        m = actions.parse_pane_menu(cap)
        self.assertEqual(m["kind"], "question")
        self.assertFalse(m.get("multi"))
        self.assertEqual([o["num"] for o in m["options"]], [1, 2, 3, 4, 5])
        self.assertEqual(m["options"][0]["label"], "A")

    def test_multiselect_picker_flags_multi_and_splits_checkboxes(self):
        # Real layout of a multiSelect AskUserQuestion: a tab strip with a
        # "\u2714 Submit" tab, checkboxes on each option, same picker footer.
        cap = (
            "\u2190  \u2610 Colors  \u2714 Submit  \u2192\n"
            "\n"
            "Which colors do you like?\n"
            "\n"
            "\u276f 1. [ ] Red\n  The color red.\n"
            "  2. [\u2714] Green\n  The color green.\n"
            "  3. [ ] Blue\n  The color blue.\n"
            "  4. [ ] Yellow\n  The color yellow.\n"
            "  5. [ ] Type something\n     Submit\n"
            "  6. Chat about this\n"
            "Enter to select \u00b7 \u2191/\u2193 to navigate \u00b7 Esc to cancel"
        )
        m = actions.parse_pane_menu(cap)
        self.assertEqual(m["kind"], "question")
        self.assertTrue(m["multi"])
        self.assertEqual([o["num"] for o in m["options"]], [1, 2, 3, 4, 5, 6])
        # checkbox prefix is stripped from the label and surfaced as `checked`
        self.assertEqual(m["options"][0]["label"], "Red")
        self.assertFalse(m["options"][0]["checked"])
        self.assertEqual(m["options"][1]["label"], "Green")
        self.assertTrue(m["options"][1]["checked"])
        # the "\u2714 Submit" tab strip is chrome, not part of the question text
        self.assertIn("Which colors do you like?", m["prompt"])
        self.assertNotIn("Submit", m["prompt"])

    def test_submit_review_screen_detected_as_picker(self):
        # After Tab on a multiSelect picker, Claude shows a footer-less review
        # screen. The current parser missed it (no "to select"/"proceed" line).
        cap = (
            "\u2190  \u2612 Colors  \u2714 Submit  \u2192\n\n"
            "Review your answers\n\n"
            " \u25cf Which colors do you like?\n   \u2192 Blue, Green\n\n"
            "Ready to submit your answers?\n\n"
            "\u276f 1. Submit answers\n  2. Cancel"
        )
        m = actions.parse_pane_menu(cap)
        self.assertEqual(m["kind"], "question")
        self.assertFalse(m.get("multi"))
        self.assertEqual([o["label"] for o in m["options"]], ["Submit answers", "Cancel"])
        self.assertIn("Ready to submit", m["prompt"])

    def test_permission_prompt(self):
        cap = (
            "Bash(rm x)\nDo you want to proceed?\n"
            "\u276f 1. Yes\n  2. Yes, and don't ask again\n"
            "  3. No, and tell Claude what to do differently (esc)"
        )
        m = actions.parse_pane_menu(cap)
        self.assertEqual(m["kind"], "permission")
        self.assertEqual(m["options"][0]["label"], "Yes")
        self.assertEqual(len(m["options"]), 3)

    def test_current_picker_isolated_from_older_one_in_scrollback(self):
        # Two pickers in scrollback; the current one's first options are "above
        # the fold". Must return ONLY the current picker, full 1..5.
        cap = (
            " \u2610 old\n\u276f 1. old-a\n  2. old-b\n  3. old-c\n"
            "  4. Type something.\n  5. Chat about this\n"
            "Enter to select \u00b7 \u2191/\u2193 to navigate \u00b7 Esc to cancel\n"
            " \u2610 current\nthe real question\n"
            "\u276f 1. cur-a\n     desc line\n  2. cur-b\n  3. cur-c\n"
            "  4. Type something.\n  5. Chat about this\n"
            "Enter to select \u00b7 \u2191/\u2193 to navigate \u00b7 Esc to cancel\n"
            "  6 tasks (1 done)"
        )
        m = actions.parse_pane_menu(cap)
        self.assertEqual([o["label"] for o in m["options"]],
                         ["cur-a", "cur-b", "cur-c", "Type something.", "Chat about this"])
        self.assertIn("the real question", m["prompt"])

    def test_non_menu_output_returns_none(self):
        self.assertIsNone(actions.parse_pane_menu("hello\n1. a list\n2. another\nnormal"))
        self.assertIsNone(actions.parse_pane_menu(""))

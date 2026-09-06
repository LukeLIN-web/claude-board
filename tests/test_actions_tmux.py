"""Tests for the tmux-backed wrappers in core/actions.py (create_session, send_prompt)."""
import contextlib
import os
import tempfile
import types
import unittest
from unittest import mock

from core import actions


def _fake_window(tty, platform="claude", session_id=None):
    return types.SimpleNamespace(tty=tty, platform=platform, session_id=session_id)


def _preflight_ok(foreground="node"):
    """A pane that's ready to be typed into.

    Before it types, `send_prompt` inspects the pane: what's in the foreground,
    whether it's in copy mode, whether a /btw aside or a cancellable dialog is
    covering the composer, and whether the composer has drawn yet. None of that
    is stubbed by a `send_text` mock, so tests that only mocked the send were
    running the whole inspection against this machine's real tmux server —
    failing on an unrelated pane, or blocking for 15s in the composer wait.

    Pass foreground=None to leave `pane_current_command` alone for tests that
    drive it themselves.
    """
    stack = contextlib.ExitStack()
    if foreground is not None:
        stack.enter_context(mock.patch.object(
            actions.tmux, "pane_current_command", return_value=foreground))
    stack.enter_context(mock.patch.object(
        actions.tmux, "exit_copy_mode", return_value={"ok": True}))
    stack.enter_context(mock.patch.object(actions, "_archive_open_aside"))
    stack.enter_context(mock.patch.object(actions, "_dismiss_answer_overlay"))
    stack.enter_context(mock.patch.object(actions, "_clear_blocker", return_value=None))
    stack.enter_context(mock.patch.object(
        actions, "_wait_composer_ready", return_value=True))
    return stack


class CreateSessionTests(unittest.TestCase):
    def setUp(self):
        # A spawn now answers the folder-trust prompt in the pane it just made.
        # These tests are about the dispatch; the poll has its own tests below.
        p = mock.patch.object(actions, "confirm_trust_prompt",
                              return_value={"answered": True, "waited": 0.3, "reason": ""})
        self.trust = p.start()
        self.addCleanup(p.stop)

    def test_spawn_answers_the_trust_prompt(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(actions.tmux, "new_window",
                                   return_value={"ok": True, "pane_id": "%1"}):
                r = actions.create_session(d)
        self.trust.assert_called_once_with("%1")
        self.assertTrue(r["trust"]["answered"])

    def test_codex_spawn_has_no_trust_prompt_to_answer(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(actions.tmux, "new_window",
                                   return_value={"ok": True, "pane_id": "%1"}):
                actions.create_session(d, platform="codex")
        self.trust.assert_not_called()

    def test_failed_spawn_is_not_polled(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(actions.tmux, "new_window",
                                   return_value={"ok": False, "error": "no server"}):
                r = actions.create_session(d)
        self.assertFalse(r["ok"])
        self.trust.assert_not_called()

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
        with _preflight_ok(), \
             mock.patch.object(actions, "find_window", return_value=_fake_window("/dev/pts/3")), \
             mock.patch.object(actions.tmux, "pane_for_tty", return_value="%5") as pf, \
             mock.patch.object(actions.tmux, "send_text", return_value={"ok": True}) as st:
            r = actions.send_prompt(1234, "hello")
        pf.assert_called_once_with("/dev/pts/3")
        # Claude's busy pane can drop the injected keystrokes, so verify the text
        # lands before Enter and that the composer empties after — anchored on
        # Claude's `❯` composer marker, never Codex's `›` (a task-list separator
        # here).
        st.assert_called_once_with(
            "%5", "hello", verify_landed=True, verify_submit=True, marker="❯",
        )
        self.assertTrue(r["ok"])

    def test_codex_window_gets_settle_before_enter(self):
        with _preflight_ok(), \
             mock.patch.object(actions, "find_window",
                               return_value=_fake_window("/dev/pts/3", platform="codex")), \
             mock.patch.object(actions.tmux, "pane_for_tty", return_value="%5"), \
             mock.patch.object(actions.tmux, "send_text", return_value={"ok": True}) as st:
            r = actions.send_prompt(1234, "hello")
        # Codex gets a length-scaled settle and submit-verification, anchored on
        # Codex's `›` composer marker.
        st.assert_called_once_with(
            "%5", "hello",
            settle_before_enter=actions.tmux.codex_enter_settle(len("hello")),
            verify_submit=True, marker="›",
        )
        self.assertTrue(r["ok"])

    def test_newlines_collapsed_to_spaces(self):
        with _preflight_ok(), \
             mock.patch.object(actions, "find_window", return_value=_fake_window("/dev/pts/3")), \
             mock.patch.object(actions.tmux, "pane_for_tty", return_value="%5"), \
             mock.patch.object(actions.tmux, "send_text", return_value={"ok": True}) as st:
            actions.send_prompt(1234, "line1\nline2\nline3")
        self.assertEqual(st.call_args[0][1], "line1 line2 line3")

    def test_shell_foreground_refuses_to_send(self):
        # If the TUI exited or was suspended, the pane's foreground process is
        # its parent shell — injected text would echo at the shell prompt and
        # the submit Enter would EXECUTE the prompt as a shell command.
        for shell in ("bash", "zsh", "-fish"):
            with mock.patch.object(actions, "find_window",
                                   return_value=_fake_window("/dev/pts/3")), \
                 mock.patch.object(actions.tmux, "pane_for_tty", return_value="%5"), \
                 mock.patch.object(actions.tmux, "pane_current_command",
                                   return_value=shell), \
                 mock.patch.object(actions.tmux, "send_text") as st:
                r = actions.send_prompt(1234, "hello")
            self.assertFalse(r["ok"], shell)
            self.assertIn("shell", r["error"])
            st.assert_not_called()

    def test_unknown_foreground_command_still_sends(self):
        # The lookup is best-effort: an unrecognized or unresolvable foreground
        # command (node, claude, "") must not block the send.
        for cmd in ("claude", "node", "", None):
            with _preflight_ok(foreground=None), \
                 mock.patch.object(actions, "find_window",
                                   return_value=_fake_window("/dev/pts/3")), \
                 mock.patch.object(actions.tmux, "pane_for_tty", return_value="%5"), \
                 mock.patch.object(actions.tmux, "pane_current_command",
                                   return_value=cmd), \
                 mock.patch.object(actions.tmux, "send_text",
                                   return_value={"ok": True}) as st:
                r = actions.send_prompt(1234, "hello")
            self.assertTrue(r["ok"], repr(cmd))
            st.assert_called_once()

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
        with _preflight_ok(), \
             mock.patch.object(actions, "find_window", return_value=_fake_window("/dev/pts/3")), \
             mock.patch.object(actions.tmux, "pane_for_tty", return_value="%5"), \
             mock.patch.object(actions.tmux, "send_text", return_value={"ok": True}) as st:
            r = actions.send_prompt(1234, ok_text)
        self.assertTrue(r["ok"])
        st.assert_called_once()

    # A settled /btw answer overlay left on the pane: ▔ border + "Esc to close".
    _BTW_OVERLAY = (
        "▔" * 60 + "\n\n"
        "    /btw what is 2 plus 2\n\n"
        "      2 plus 2 is 4.\n\n"
        "    ↑/↓ to scroll · c to copy · f to fork · Esc to close\n"
    )
    _CLEAN_PANE = "❯ \n⏵⏵ bypass permissions on"

    def test_open_btw_overlay_is_dismissed_before_send(self):
        # An open /btw overlay is modal: a prompt pasted while it's up is eaten.
        # send_prompt must Escape it (then re-check it cleared) before the paste.
        caps = [{"ok": True, "text": self._BTW_OVERLAY},
                {"ok": True, "text": self._CLEAN_PANE}]
        with mock.patch.object(actions, "find_window", return_value=_fake_window("/dev/pts/3")), \
             mock.patch.object(actions.tmux, "pane_for_tty", return_value="%5"), \
             mock.patch.object(actions.tmux, "capture_pane",
                               side_effect=lambda *a, **k: caps.pop(0) if caps else {"ok": True, "text": self._CLEAN_PANE}), \
             mock.patch.object(actions.tmux, "send_keys", return_value={"ok": True}) as sk, \
             mock.patch.object(actions.tmux, "send_text", return_value={"ok": True}) as st, \
             mock.patch.object(actions.time, "sleep"):
            r = actions.send_prompt(1234, "hello")
        sk.assert_any_call("%5", "Escape")
        st.assert_called_once()
        self.assertTrue(r["ok"])

    def test_no_escape_when_composer_is_clean(self):
        # No overlay -> never touch Escape (it would interrupt a working session).
        with mock.patch.object(actions, "find_window", return_value=_fake_window("/dev/pts/3")), \
             mock.patch.object(actions.tmux, "pane_for_tty", return_value="%5"), \
             mock.patch.object(actions.tmux, "capture_pane",
                               return_value={"ok": True, "text": self._CLEAN_PANE}), \
             mock.patch.object(actions.tmux, "send_keys", return_value={"ok": True}) as sk, \
             mock.patch.object(actions.tmux, "send_text", return_value={"ok": True}) as st:
            r = actions.send_prompt(1234, "hello")
        sk.assert_not_called()
        st.assert_called_once()
        self.assertTrue(r["ok"])

    # The same aside mid-generation: footer lacks "c to copy" (not settled).
    _BTW_ANSWERING = (
        "▔" * 60 + "\n\n"
        "    /btw what is 2 plus 2\n\n"
        "      ✽ Answering…\n\n"
        "    ↑/↓ to scroll · Esc to close\n"
    )

    # Current Claude builds (v2.1.20x) draw the same overlay with NO ▔ top
    # border: composer echo of the command, the question stack, the answer,
    # the footer. Lifted from a live pane.
    _BTW_OVERLAY_BORDERLESS = (
        "❯ /btw what is 2 plus 2\n\n"
        "  /btw what is 2 plus 2\n\n"
        "    2 plus 2 is 4.\n\n"
        "  ↑/↓ to scroll · c to copy · f to fork · Esc to close\n"
    )
    _BTW_ANSWERING_BORDERLESS = (
        "❯ /btw what is 2 plus 2\n\n"
        "  /btw what is 2 plus 2\n\n"
        "    · Answering…\n\n"
        "  Esc to close\n"
    )

    def test_borderless_overlay_is_dismissed_before_send(self):
        # Current builds draw no ▔ border. Missing the overlay here means the
        # pasted prompt is eaten by the overlay's key handling, which destroys
        # the aside AND silently loses the prompt (seen live: both /btw answers
        # and the follow-up prompt vanished).
        caps = [{"ok": True, "text": self._BTW_OVERLAY_BORDERLESS},
                {"ok": True, "text": self._CLEAN_PANE}]
        with mock.patch.object(actions, "find_window", return_value=_fake_window("/dev/pts/3")), \
             mock.patch.object(actions.tmux, "pane_for_tty", return_value="%5"), \
             mock.patch.object(actions.tmux, "capture_pane",
                               side_effect=lambda *a, **k: caps.pop(0) if caps else {"ok": True, "text": self._CLEAN_PANE}), \
             mock.patch.object(actions.tmux, "send_keys", return_value={"ok": True}) as sk, \
             mock.patch.object(actions.tmux, "send_text", return_value={"ok": True}) as st, \
             mock.patch.object(actions.time, "sleep"):
            r = actions.send_prompt(1234, "hello")
        sk.assert_any_call("%5", "Escape")
        st.assert_called_once()
        self.assertTrue(r["ok"])

    def test_borderless_answering_aside_is_waited_out_and_archived(self):
        from core import btwcapture
        caps = [{"ok": True, "text": self._BTW_ANSWERING_BORDERLESS},  # archive: generating
                {"ok": True, "text": self._BTW_OVERLAY_BORDERLESS},    # archive: settled -> latch
                {"ok": True, "text": self._BTW_OVERLAY_BORDERLESS},    # dismiss: present -> Esc
                {"ok": True, "text": self._CLEAN_PANE}]                # dismiss: cleared
        with mock.patch.object(actions, "find_window",
                               return_value=_fake_window("/dev/pts/3", session_id="sessX")), \
             mock.patch.object(actions.tmux, "pane_for_tty", return_value="%5"), \
             mock.patch.object(actions.tmux, "capture_pane",
                               side_effect=lambda *a, **k: caps.pop(0) if caps else {"ok": True, "text": self._CLEAN_PANE}), \
             mock.patch.object(actions.tmux, "send_keys", return_value={"ok": True}) as sk, \
             mock.patch.object(actions.tmux, "send_text", return_value={"ok": True}), \
             mock.patch.object(actions.time, "sleep"), \
             mock.patch.object(btwcapture, "capture_sync") as cs:
            r = actions.send_prompt(1234, "hello")
        cs.assert_called_once_with(1234, "sessX")
        sk.assert_any_call("%5", "Escape")
        self.assertTrue(r["ok"])

    def test_answering_aside_is_waited_out_and_archived_before_dismiss(self):
        # The aside's answer exists only in the overlay. send_prompt must wait
        # (bounded) for it to settle and archive it BEFORE the dismiss-Escape
        # destroys it — otherwise the answer is unrecoverable.
        from core import btwcapture
        caps = [{"ok": True, "text": self._BTW_ANSWERING},   # archive: still generating
                {"ok": True, "text": self._BTW_OVERLAY},     # archive: settled -> latch
                {"ok": True, "text": self._BTW_OVERLAY},     # dismiss: overlay present -> Esc
                {"ok": True, "text": self._CLEAN_PANE}]      # dismiss: cleared
        with mock.patch.object(actions, "find_window",
                               return_value=_fake_window("/dev/pts/3", session_id="sessX")), \
             mock.patch.object(actions.tmux, "pane_for_tty", return_value="%5"), \
             mock.patch.object(actions.tmux, "capture_pane",
                               side_effect=lambda *a, **k: caps.pop(0) if caps else {"ok": True, "text": self._CLEAN_PANE}), \
             mock.patch.object(actions.tmux, "send_keys", return_value={"ok": True}) as sk, \
             mock.patch.object(actions.tmux, "send_text", return_value={"ok": True}), \
             mock.patch.object(actions.time, "sleep"), \
             mock.patch.object(btwcapture, "capture_sync") as cs:
            r = actions.send_prompt(1234, "hello")
        cs.assert_called_once_with(1234, "sessX")
        sk.assert_any_call("%5", "Escape")
        self.assertTrue(r["ok"])

    def test_no_archive_without_session_id(self):
        # Windows without a session id (e.g. codex) have no /btw archive to feed.
        from core import btwcapture
        caps = [{"ok": True, "text": self._BTW_OVERLAY},
                {"ok": True, "text": self._CLEAN_PANE}]
        with mock.patch.object(actions, "find_window", return_value=_fake_window("/dev/pts/3")), \
             mock.patch.object(actions.tmux, "pane_for_tty", return_value="%5"), \
             mock.patch.object(actions.tmux, "capture_pane",
                               side_effect=lambda *a, **k: caps.pop(0) if caps else {"ok": True, "text": self._CLEAN_PANE}), \
             mock.patch.object(actions.tmux, "send_keys", return_value={"ok": True}), \
             mock.patch.object(actions.tmux, "send_text", return_value={"ok": True}), \
             mock.patch.object(actions.time, "sleep"), \
             mock.patch.object(btwcapture, "capture_sync") as cs:
            r = actions.send_prompt(1234, "hello")
        cs.assert_not_called()
        self.assertTrue(r["ok"])


class SendPromptReadinessTests(unittest.TestCase):
    """send_prompt must not type until a composer marker is on screen.

    Typing into a still-booting TUI is lost or replayed doubled (the retry's
    C-u sits unread in the same pty buffer as the text it should clear), and a
    pane wedged on the Rewind panel (double-Escape — e.g. the board's own Esc
    button pressed twice on an idle card) eats every send until dismissed.
    """

    # A freshly spawned Claude still booting: banner painted, no composer yet.
    _BOOTING = (
        " ▐▛███▜▌   Claude Code v2.1.211\n"
        "▝▜█████▛▘  Fable 5 · Claude Max\n"
        "  ▘▘ ▝▝    /shared/ws/proj\n"
    )
    _READY = _BOOTING + "❯ \n⏵⏵ bypass permissions on"
    # The Rewind panel replacing the composer (lifted from a live wedged pane).
    _REWIND = _BOOTING + "  Rewind\n\n  Nothing to rewind to yet.\n\n  Esc to cancel\n"
    # Same panel in a session WITH checkpoints (lifted live): it draws its own
    # `❯ (current)` cursor row, so the last on-screen ❯ is inside the panel.
    _REWIND_HISTORY = (
        "❯ earlier prompt\n\n● OK\n\n"
        "  Rewind\n\n"
        "  Restore the code and/or conversation to the point before…\n"
        "    earlier prompt\n"
        "    No code changes\n"
        "  ❯ (current)\n\n"
        "  Enter to continue · Esc to cancel\n"
    )

    def _send(self, caps, text="hello"):
        seq = list(caps)
        last = seq[-1]
        with mock.patch.object(actions, "find_window", return_value=_fake_window("/dev/pts/3")), \
             mock.patch.object(actions.tmux, "pane_for_tty", return_value="%5"), \
             mock.patch.object(actions.tmux, "exit_copy_mode") as ecm, \
             mock.patch.object(actions.tmux, "capture_pane",
                               side_effect=lambda *a, **k: {"ok": True, "text": seq.pop(0) if seq else last}), \
             mock.patch.object(actions.tmux, "send_keys", return_value={"ok": True}) as sk, \
             mock.patch.object(actions.tmux, "send_text", return_value={"ok": True}) as st, \
             mock.patch.object(actions.time, "sleep"), \
             mock.patch.object(actions, "_COMPOSER_READY_TIMEOUT", 0.2), \
             mock.patch.object(actions, "_COMPOSER_READY_POLL", 0.0):
            r = actions.send_prompt(1234, text)
        return r, sk, st, ecm

    def test_booting_pane_waits_for_composer_then_sends(self):
        # dismiss-overlay probe sees the booting pane, then the readiness loop
        # sees it once more before the composer paints.
        r, sk, st, _ = self._send([self._BOOTING, self._BOOTING, self._READY])
        st.assert_called_once()
        sk.assert_not_called()
        self.assertTrue(r["ok"])

    def test_composer_never_ready_fails_without_typing(self):
        r, _, st, _ = self._send([self._BOOTING])
        st.assert_not_called()
        self.assertFalse(r["ok"])
        self.assertIn("composer", r["error"])

    def test_rewind_panel_is_escaped_then_send_proceeds(self):
        r, sk, st, _ = self._send([self._REWIND, self._REWIND, self._READY])
        sk.assert_any_call("%5", "Escape")
        st.assert_called_once()
        self.assertTrue(r["ok"])

    def test_rewind_panel_with_checkpoints_is_escaped_despite_its_own_cursor(self):
        # The ❯ cursor row inside the panel must not read as a ready composer.
        r, sk, st, _ = self._send(
            [self._REWIND_HISTORY, self._REWIND_HISTORY, self._READY])
        sk.assert_any_call("%5", "Escape")
        st.assert_called_once()
        self.assertTrue(r["ok"])

    # Newer builds (seen live on v2.1.216, fixtures/rewind_panel_no_footer.txt)
    # draw NO footer while the panel cursor sits on "(current)": nothing to
    # restore means no "Enter to continue · Esc to cancel" line, so the cursor
    # row itself is the last thing on screen.
    _REWIND_NO_FOOTER = (
        "  建议的第一步:不必先吞 1.82 TB。\n\n"
        "✻ Cooked for 5m 2s\n\n"
        "────────────────────────────────────────\n"
        "  Rewind\n\n"
        "  Restore the code and/or conversation to the point before…\n\n"
        "    reference 精读一下, https://arxiv.org/abs/2607.17423 , 看看可以作为我…\n"
        "    TimeLens2.md +60\n\n"
        "  ❯ (current)\n\n\n"
    )

    def test_footerless_rewind_panel_is_escaped_then_send_proceeds(self):
        r, sk, st, _ = self._send(
            [self._REWIND_NO_FOOTER, self._REWIND_NO_FOOTER, self._READY])
        sk.assert_any_call("%5", "Escape")
        st.assert_called_once()
        self.assertTrue(r["ok"])

    def test_copy_mode_is_cancelled_before_typing(self):
        r, _, st, ecm = self._send([self._READY])
        ecm.assert_called_once_with("%5")
        st.assert_called_once()
        self.assertTrue(r["ok"])

    def test_ready_pane_sends_without_waiting_or_keys(self):
        r, sk, st, _ = self._send([self._READY])
        sk.assert_not_called()
        st.assert_called_once()
        self.assertTrue(r["ok"])


class SendPromptBlockerTests(unittest.TestCase):
    """A cancellable overlay covering the composer is auto-Escaped and the send
    retried; the outcome names what was closed. The /model dialog is the case
    that motivated this — it draws its own ❯ cursor, so the readiness check alone
    would type straight into it and the prompt would silently vanish."""

    # A /model dialog (footer + its own ❯ cursor row), lifted from a live pane.
    _MODEL_DIALOG = (
        "  Select model\n"
        "  ❯ 2. Opus ✔  Opus 4.8 with 1M context\n"
        "  Enter to set as default · s to use this session only · Esc to cancel\n"
    )
    # A live permission menu — Esc here means deny, so the note must warn.
    _PERM_MENU = (
        "  Do you want to proceed?\n"
        "  ❯ 1. Yes\n"
        "    2. No\n"
    )
    _CLEAN = SendPromptTests._CLEAN_PANE

    def _send_with_blocker(self, blocker_text, clears=True, text="hello"):
        """Drive send_prompt with `blocker_text` on the pane. When `clears`, the
        pane goes clean once an Escape is delivered (a dialog that dismisses); when
        not, it stays blocked no matter how many Escapes land."""
        state = {"escaped": False}

        def cap(*a, **k):
            clean = clears and state["escaped"]
            return {"ok": True, "text": self._CLEAN if clean else blocker_text}

        def keys(pane, *ks):
            if ks and ks[0] == "Escape":
                state["escaped"] = True
            return {"ok": True}

        with mock.patch.object(actions, "find_window", return_value=_fake_window("/dev/pts/3")), \
             mock.patch.object(actions.tmux, "pane_for_tty", return_value="%5"), \
             mock.patch.object(actions.tmux, "exit_copy_mode"), \
             mock.patch.object(actions.tmux, "capture_pane", side_effect=cap), \
             mock.patch.object(actions.tmux, "send_keys", side_effect=keys) as sk, \
             mock.patch.object(actions.tmux, "send_text", return_value={"ok": True}) as st, \
             mock.patch.object(actions.time, "sleep"), \
             mock.patch.object(actions, "_COMPOSER_READY_TIMEOUT", 0.2), \
             mock.patch.object(actions, "_COMPOSER_READY_POLL", 0.0):
            r = actions.send_prompt(1234, text)
        return r, sk, st

    def test_model_dialog_is_escaped_then_send_delivers_with_note(self):
        r, sk, st = self._send_with_blocker(self._MODEL_DIALOG)
        sk.assert_any_call("%5", "Escape")
        st.assert_called_once()
        self.assertTrue(r["ok"])
        self.assertIn("/model dialog", r["note"])
        self.assertIn("auto-closed", r["note"])
        self.assertNotIn("⚠", r["note"])  # cancelling /model is not destructive

    def test_permission_menu_autoclose_note_warns_about_denial(self):
        r, sk, st = self._send_with_blocker(self._PERM_MENU)
        sk.assert_any_call("%5", "Escape")
        st.assert_called_once()
        self.assertTrue(r["ok"])
        self.assertIn("permission/choice menu", r["note"])
        self.assertIn("⚠", r["note"])  # Esc denied a pending tool call

    def test_blocker_that_wont_clear_reports_specific_error_without_typing(self):
        r, sk, st = self._send_with_blocker(self._MODEL_DIALOG, clears=False)
        self.assertFalse(r["ok"])
        self.assertIn("/model dialog", r["error"])
        self.assertIn("still open", r["error"])
        st.assert_not_called()  # never type into a pane still covered by a dialog

    def test_reactive_diagnosis_names_blocker_when_send_fails(self):
        # Composer looks clear, so the send is attempted, but it doesn't land and
        # a dialog is on screen by the time we re-capture — name it, don't report
        # the generic "never landed".
        state = {"sent": False}

        def cap(*a, **k):
            return {"ok": True,
                    "text": self._MODEL_DIALOG if state["sent"] else self._CLEAN}

        def stext(*a, **k):
            state["sent"] = True
            return {"ok": False, "error": "prompt text never landed in composer"}

        with mock.patch.object(actions, "find_window", return_value=_fake_window("/dev/pts/3")), \
             mock.patch.object(actions.tmux, "pane_for_tty", return_value="%5"), \
             mock.patch.object(actions.tmux, "exit_copy_mode"), \
             mock.patch.object(actions.tmux, "capture_pane", side_effect=cap), \
             mock.patch.object(actions.tmux, "send_keys", return_value={"ok": True}), \
             mock.patch.object(actions.tmux, "send_text", side_effect=stext) as st, \
             mock.patch.object(actions.time, "sleep"):
            r = actions.send_prompt(1234, "hello")
        st.assert_called_once()
        self.assertFalse(r["ok"])
        self.assertIn("/model dialog", r["error"])
        self.assertNotIn("never landed", r["error"])


class SendFailureDiagnosisTests(unittest.TestCase):
    """A landed-verify failure with no classifiable blocker must still say what
    the pane looked like — a booting TUI, a busy pane dropping keystrokes, and
    an unrecognized overlay used to collapse into the same generic
    "prompt text never landed in composer"."""

    _CLEAN = SendPromptTests._CLEAN_PANE
    _BOOTING = SendPromptReadinessTests._BOOTING
    _REWIND_NO_FOOTER = SendPromptReadinessTests._REWIND_NO_FOOTER

    def _fail_send(self, after_text):
        """Composer looks clean pre-send; the send doesn't land; `after_text` is
        on the pane by the time the failure is diagnosed."""
        state = {"sent": False}

        def cap(*a, **k):
            return {"ok": True,
                    "text": after_text if state["sent"] else self._CLEAN}

        def stext(*a, **k):
            state["sent"] = True
            return {"ok": False, "error": "prompt text never landed in composer"}

        with mock.patch.object(actions, "find_window", return_value=_fake_window("/dev/pts/3")), \
             mock.patch.object(actions.tmux, "pane_for_tty", return_value="%5"), \
             mock.patch.object(actions.tmux, "exit_copy_mode"), \
             mock.patch.object(actions.tmux, "capture_pane", side_effect=cap), \
             mock.patch.object(actions.tmux, "send_keys", return_value={"ok": True}), \
             mock.patch.object(actions.tmux, "send_text", side_effect=stext), \
             mock.patch.object(actions.time, "sleep"):
            return actions.send_prompt(1234, "hello")

    def test_footerless_rewind_panel_is_named_not_generic(self):
        r = self._fail_send(self._REWIND_NO_FOOTER)
        self.assertFalse(r["ok"])
        self.assertIn("Rewind panel", r["error"])
        self.assertNotIn("never landed", r["error"])

    def test_no_composer_marker_is_reported_as_such(self):
        r = self._fail_send(self._BOOTING)
        self.assertFalse(r["ok"])
        self.assertIn("never landed", r["error"])
        self.assertIn("no composer marker", r["error"])

    def test_empty_composer_reports_dropped_keystrokes(self):
        r = self._fail_send(self._CLEAN)
        self.assertFalse(r["ok"])
        self.assertIn("never landed", r["error"])
        self.assertIn("empty", r["error"])

    def test_composer_holding_other_text_is_quoted(self):
        r = self._fail_send("❯ some other draft\n⏵⏵ bypass permissions on")
        self.assertFalse(r["ok"])
        self.assertIn("never landed", r["error"])
        self.assertIn("some other draft", r["error"])


class SendMenuKeysOverlayTests(unittest.TestCase):
    """The dashboard Esc button closes a /btw overlay as a side effect; a settled
    un-archived answer must be latched before that Escape is delivered."""

    _BTW_OVERLAY = SendPromptTests._BTW_OVERLAY
    _BTW_ANSWERING = SendPromptTests._BTW_ANSWERING
    _CLEAN_PANE = SendPromptTests._CLEAN_PANE

    def _run(self, keys, pane_text, session_id="sessX"):
        from core import btwcapture
        with mock.patch.object(actions, "find_window",
                               return_value=_fake_window("/dev/pts/3", session_id=session_id)), \
             mock.patch.object(actions.tmux, "pane_for_tty", return_value="%5"), \
             mock.patch.object(actions.tmux, "capture_pane",
                               return_value={"ok": True, "text": pane_text}), \
             mock.patch.object(actions.tmux, "send_keys", return_value={"ok": True}) as sk, \
             mock.patch.object(btwcapture, "capture_sync") as cs:
            r = actions.send_menu_keys(1234, keys)
        return r, sk, cs

    def test_escape_archives_settled_overlay_first(self):
        r, sk, cs = self._run(["Escape"], self._BTW_OVERLAY)
        cs.assert_called_once_with(1234, "sessX")
        sk.assert_called_once_with("%5", "Escape")
        self.assertTrue(r["ok"])

    def test_escape_does_not_wait_for_generating_aside(self):
        # The user is interrupting; delaying their Escape would be worse than
        # losing the aside they chose to kill.
        r, sk, cs = self._run(["Escape"], self._BTW_ANSWERING)
        cs.assert_not_called()
        sk.assert_called_once_with("%5", "Escape")
        self.assertTrue(r["ok"])

    def test_non_escape_keys_never_probe_the_pane(self):
        r, sk, cs = self._run(["1"], self._BTW_OVERLAY)
        cs.assert_not_called()
        sk.assert_called_once_with("%5", "1")
        self.assertTrue(r["ok"])


class SendMenuKeysInterruptEscalationTests(unittest.TestCase):
    """A bare Esc that Claude Code's own interrupt can't honour (turn wedged on a
    D-state child) escalates to a SIGKILL of the Bash-tool wrapper."""

    _CLEAN_PANE = SendPromptTests._CLEAN_PANE

    def _run(self, keys, wrappers, send_ok=True):
        """`wrappers` is a list of returns for successive uninterruptible_wrappers
        calls (the pre-check, then the post-settle re-check)."""
        import signal
        with mock.patch.object(actions, "find_window",
                               return_value=_fake_window("/dev/pts/3", session_id="s")), \
             mock.patch.object(actions.tmux, "pane_for_tty", return_value="%5"), \
             mock.patch.object(actions.tmux, "capture_pane",
                               return_value={"ok": True, "text": self._CLEAN_PANE}), \
             mock.patch.object(actions.tmux, "send_keys",
                               return_value={"ok": send_ok}) as sk, \
             mock.patch.object(actions, "uninterruptible_wrappers",
                               side_effect=wrappers) as uw, \
             mock.patch.object(actions.time, "sleep") as sleep, \
             mock.patch.object(actions.os, "kill") as kill:
            r = actions.send_menu_keys(1234, keys)
        return r, sk, uw, sleep, kill, signal

    def test_wedged_wrapper_is_force_killed(self):
        r, sk, uw, sleep, kill, signal = self._run(["Escape"], [[200], [200]])
        sk.assert_called_once_with("%5", "Escape")   # graceful Esc still sent first
        sleep.assert_called_once()                   # gave the graceful path a beat
        kill.assert_called_once_with(200, signal.SIGKILL)
        self.assertTrue(r["escalated"])
        self.assertEqual(r["killed_wrappers"], [200])

    def test_no_dwrapper_never_escalates_and_stays_fast(self):
        r, sk, uw, sleep, kill, _ = self._run(["Escape"], [[]])
        sk.assert_called_once_with("%5", "Escape")
        sleep.assert_not_called()                    # no settle in the common case
        kill.assert_not_called()
        self.assertNotIn("escalated", r)
        self.assertTrue(r["ok"])

    def test_graceful_interrupt_clearing_after_settle_kills_nothing(self):
        # Pre-check sees the wedge; after the settle it's gone (graceful Esc won).
        r, sk, uw, sleep, kill, _ = self._run(["Escape"], [[200], []])
        sleep.assert_called_once()
        kill.assert_not_called()
        self.assertNotIn("escalated", r)

    def test_non_escape_key_never_escalates(self):
        r, sk, uw, sleep, kill, _ = self._run(["1"], [[200], [200]])
        uw.assert_not_called()
        kill.assert_not_called()
        self.assertNotIn("escalated", r)

    def test_escape_combined_with_other_keys_is_not_an_interrupt(self):
        # Only a bare ["Escape"] is the interrupt button; combos are picker nav.
        r, sk, uw, sleep, kill, _ = self._run(["1", "Escape"], [[200], [200]])
        uw.assert_not_called()
        kill.assert_not_called()

    def test_failed_send_skips_escalation(self):
        r, sk, uw, sleep, kill, _ = self._run(["Escape"], [[200], [200]], send_ok=False)
        uw.assert_not_called()
        kill.assert_not_called()
        self.assertFalse(r["ok"])


_PICKER_TEXT = (
    "This session is 7h old and 192k tokens.\n"
    "  ❯ 1. Resume from summary (recommended)\n"
    "    2. Resume full session as-is\n"
    "    3. Don't ask me again\n"
    "  Enter to confirm · Esc to cancel"
)
_LIVE_TEXT = "❯ \n⏵⏵ bypass permissions on"


class ConfirmResumePickerTests(unittest.TestCase):
    """Auto-answering Claude's 'resume from summary?' picker for fleet resumes."""

    def test_digit_then_enter_confirms_and_reports(self):
        # Picker is up at first; still up after the digit (digit only selects);
        # gone after Enter. Expect choice "2" then Enter, and confirmed=True.
        caps = [_PICKER_TEXT, _PICKER_TEXT, _LIVE_TEXT]

        def fake_capture(pane, **kw):
            return {"ok": True, "text": caps.pop(0) if caps else _LIVE_TEXT}

        sent = []
        with mock.patch.object(actions.tmux, "capture_pane", side_effect=fake_capture), \
             mock.patch.object(actions.tmux, "send_keys", side_effect=lambda p, *k: sent.append(k)), \
             mock.patch.object(actions.time, "sleep"):
            r = actions.confirm_resume_picker("%0")
        self.assertTrue(r["confirmed"])
        self.assertEqual(sent, [("2",), ("Enter",)])

    def test_no_enter_leak_when_digit_already_dismissed(self):
        # Some builds confirm on the digit alone: the picker is gone right after
        # "2", so Enter must NOT be sent (it would land in the live session).
        caps = [_PICKER_TEXT, _LIVE_TEXT]

        def fake_capture(pane, **kw):
            return {"ok": True, "text": caps.pop(0) if caps else _LIVE_TEXT}

        sent = []
        with mock.patch.object(actions.tmux, "capture_pane", side_effect=fake_capture), \
             mock.patch.object(actions.tmux, "send_keys", side_effect=lambda p, *k: sent.append(k)), \
             mock.patch.object(actions.time, "sleep"):
            r = actions.confirm_resume_picker("%0")
        self.assertTrue(r["confirmed"])
        self.assertEqual(sent, [("2",)])

    def test_no_picker_sends_nothing(self):
        # A small session resumes straight to a live prompt: never send keys.
        with mock.patch.object(actions.tmux, "capture_pane",
                               return_value={"ok": True, "text": _LIVE_TEXT}), \
             mock.patch.object(actions.tmux, "send_keys") as sk, \
             mock.patch.object(actions.time, "sleep"):
            r = actions.confirm_resume_picker("%0", attempts=2)
        self.assertFalse(r["confirmed"])
        self.assertEqual(r["reason"], "no picker")
        sk.assert_not_called()


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

    def test_resume_summary_confirm_picker(self):
        # Startup "resume from summary?" picker: footer is "Enter to confirm",
        # and the transcript text above the divider rule must NOT leak into the
        # prompt \u2014 only the lines between the rule and the options.
        cap = (
            "  #### Active Tasks\n"
            "  | 0 | some stale transcript row that must be excluded |\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "  This session is 4h 21m old and 264.3k tokens.\n\n"
            "  We recommend resuming from a summary.\n\n"
            "  \u276f 1. Resume from summary (recommended)\n"
            "    2. Resume full session as-is\n"
            "    3. Don't ask me again\n\n"
            "  Enter to confirm \u00b7 Esc to cancel"
        )
        m = actions.parse_pane_menu(cap)
        self.assertEqual(m["kind"], "question")
        self.assertFalse(m.get("multi"))
        self.assertEqual([o["num"] for o in m["options"]], [1, 2, 3])
        self.assertEqual(m["options"][0]["label"], "Resume from summary (recommended)")
        self.assertIn("This session is 4h 21m old", m["prompt"])
        self.assertNotIn("stale transcript row", m["prompt"])

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

    def test_side_by_side_preview_box_is_stripped(self):
        # AskUserQuestion options with previews render side-by-side: the option
        # list on the left, a box-drawn preview panel on the right that Claude
        # folds with "✂ N lines hidden". Captured into one pane, each option
        # row also carries the panel border; it must not leak into the labels.
        cap = (
            " ☐ Rung definition\n\n"
            "What does the cheapest-correct sweep actually produce as its short/long\n"
            "rungs — fixed absolute token caps, or per-item-adaptive caps?\n\n"
            " 1. Per-item adaptive             ┌────────┐\n"
            "   (two-pass)                     │ rung space = full │\n"
            " 2. Fixed absolute ladder         ├─ ✂ ─ 5 lines hidden ─┤\n"
            "   (as-is)                        └────────┘\n\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel"
        )
        m = actions.parse_pane_menu(cap)
        self.assertEqual(m["kind"], "question")
        self.assertEqual([o["label"] for o in m["options"]],
                         ["Per-item adaptive", "Fixed absolute ladder"])
        # box-drawing chrome and the fold marker never reach the dashboard
        self.assertNotIn("✂", m["options"][0]["label"])
        self.assertNotIn("┌", m["options"][0]["label"])
        self.assertNotIn("hidden", m["prompt"])
        # the full-width question text above the box is preserved, not cropped
        self.assertIn("per-item-adaptive caps?", m["prompt"])

    def test_non_menu_output_returns_none(self):
        self.assertIsNone(actions.parse_pane_menu("hello\n1. a list\n2. another\nnormal"))
        self.assertIsNone(actions.parse_pane_menu(""))


class PaneMenuActiveTests(unittest.TestCase):
    """pane_menu_active: pane-level ground truth for whether a session's
    "waiting / dialog open" registry status is actionable. Claude writes
    waitingFor="dialog open" for ANY overlay — including the /goal panel,
    which has nothing to answer — so the dashboard must verify the pane."""

    GOAL_OVERLAY = (
        "──────────────\n"
        "  Goal\n\n"
        "  No goal set\n"
        "  /goal <condition> to set one\n\n"
        "  Esc to dismiss\n"
        "──────────────\n"
        "❯ \n"
        "  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt"
    )
    PERMISSION = (
        "Bash(rm x)\nDo you want to proceed?\n"
        "❯ 1. Yes\n  2. No, and tell Claude what to do differently (esc)"
    )
    PICKER = (
        "Which one?\n❯ 1. A\n  2. B\n"
        "Enter to select · ↑/↓ to navigate · Esc to cancel"
    )
    RESUME_PICKER = (
        "  This session is 4h 25m old and 264.3k tokens.\n\n"
        "  ❯ 1. Resume from summary (recommended)\n"
        "    2. Resume full session as-is\n"
        "    3. Don't ask me again\n\n"
        "  Enter to confirm · Esc to cancel"
    )

    def _run(self, text, pane="%9", ok=True):
        with mock.patch.object(actions.tmux, "pane_for_tty", return_value=pane), \
             mock.patch.object(actions.tmux, "capture_pane",
                               return_value={"ok": ok, "text": text}):
            return actions.pane_menu_active("/dev/pts/9")

    def test_goal_overlay_is_not_an_active_menu(self):
        self.assertIs(self._run(self.GOAL_OVERLAY), False)

    def test_permission_prompt_is_active(self):
        self.assertIs(self._run(self.PERMISSION), True)

    def test_question_picker_is_active(self):
        self.assertIs(self._run(self.PICKER), True)

    def test_resume_summary_picker_is_active(self):
        self.assertIs(self._run(self.RESUME_PICKER), True)

    def test_no_tty_is_unknown(self):
        self.assertIsNone(actions.pane_menu_active(None))

    def test_no_pane_is_unknown(self):
        self.assertIsNone(self._run(self.GOAL_OVERLAY, pane=None))

    def test_failed_capture_is_unknown(self):
        self.assertIsNone(self._run(self.GOAL_OVERLAY, ok=False))


# A real capture of Claude's /model dialog (v2.1.209), cursor on the session's
# current model — Sonnet here, which is exactly why the row to reach can't be
# found by a fixed number of keypresses.
MODEL_DIALOG = """\
❯ /model
  Select model
  Switch between Claude models. Your pick becomes the default for new sessions.
    1. Default (recommended)  Opus 4.8 with 1M context · Best for everyday, complex tasks
    2. Opus                   Opus 4.8 with 1M context · Best for everyday, complex tasks
    3. Fable                  Fable 5 · Most capable for your hardest and longest-running tasks
  ❯ 4. Sonnet ✔               Sonnet 5 · Efficient for routine tasks
    5. Haiku                  Haiku 4.5 · Fastest for quick answers
  ● High effort (default) ←/→ to adjust
  Enter to set as default · s to use this session only · Esc to cancel
"""


# The same dialog in an 80x24 pane (v2.1.259) — the size the board's sessions
# actually run at. The list becomes a scrolling window: Default is off the top,
# the boundary rows carry ↑/↓ where the cursor would go, and what didn't fit is
# folded into "… +N model".
MODEL_DIALOG_SCROLLED = """\
  Select model
  Switch between Claude models. Your pick becomes the default for new
  sessions. For other/previous model names, specify with --model.

  ↑ 2. Opus (1M context)      Opus 5 with 1M context · Best for everyday,
                              complex tasks
    3. Fable                  Fable 5.1 · Most capable for your hardest and
                              longest-running tasks
    4. Sonnet                 Sonnet 5 · Efficient for routine tasks
    5. Haiku                  Haiku 4.5 · Fastest for quick answers
  ❯ 6. Opus ✔                 Opus 5 · Best for everyday, complex tasks
     … +1 model

  ◉ xHigh effort ←/→ to adjust

  Enter to set as default · s to use this session only · Esc to cancel
"""


class ModelDialogParseTests(unittest.TestCase):
    def test_rows_and_cursor(self):
        rows, cursor = actions._model_dialog_rows(MODEL_DIALOG)
        self.assertEqual(
            rows,
            [(1, "Default (recommended)"), (2, "Opus"), (3, "Fable"),
             (4, "Sonnet"), (5, "Haiku")],
        )
        self.assertEqual(cursor, 4)

    def test_no_cursor_when_dialog_absent(self):
        rows, cursor = actions._model_dialog_rows("no dialog here\n")
        self.assertEqual(rows, [])
        self.assertEqual(cursor, 0)

    def test_scroll_marker_rows_are_rows(self):
        # ↑/↓ sit in the cursor's column. Reading them as anything but a row is
        # how the row under the fold went missing from the offered list.
        rows, cursor = actions._model_dialog_rows(MODEL_DIALOG_SCROLLED)
        self.assertEqual(
            rows,
            [(2, "Opus (1M context)"), (3, "Fable"), (4, "Sonnet"),
             (5, "Haiku"), (6, "Opus")],
        )
        self.assertEqual(cursor, 6)

    def test_fold_counter_is_not_a_row(self):
        rows, _ = actions._model_dialog_rows("     … +4 models\n")
        self.assertEqual(rows, [])


class PickModelRowTests(unittest.TestCase):
    """A session on a model the base list doesn't carry gets a second row for its
    own family, so "Opus" is two different rows and the alias has to land on the
    one the board's dropdown means."""

    _ROWS = {1: "Default (recommended)", 2: "Opus (1M context)", 3: "Fable",
             4: "Sonnet", 5: "Haiku", 6: "Opus"}

    def test_whole_name_wins_over_first_word(self):
        self.assertEqual(actions._pick_model_row(self._ROWS, "opus"), 6)

    def test_first_word_is_the_fallback(self):
        # Nothing is named bare "Default" — the fallback is the only way in.
        self.assertEqual(actions._pick_model_row(self._ROWS, "default"), 1)

    def test_first_word_still_reaches_a_lone_variant_row(self):
        rows = {n: name for n, name in self._ROWS.items() if n != 6}
        self.assertEqual(actions._pick_model_row(rows, "opus"), 2)

    def test_absent_model_is_zero(self):
        self.assertEqual(actions._pick_model_row(self._ROWS, "gpt5"), 0)


class SwitchModelTests(unittest.TestCase):
    """switch_model drives the dialog by keypress, so the tests assert on the keys
    it sends — Enter would save the pick as the user's default, "s" must not."""

    def _drive(self, alias, cursor_start=4, rows=5, names=None, window=None,
               confirm=False, confirm_cursor=1, confirm_sticks=False):
        """Fake a dialog whose cursor wraps 1..rows and moves on each Down.

        `window` caps how many rows the dialog draws at once, the way Claude does
        in a short pane: the rest is folded behind "… +N models" and the boundary
        rows carry ↑/↓ in the cursor's column. Left None the whole list is drawn.

        `confirm` adds the second dialog Claude raises when the switch happens
        mid-conversation ("Switch model?" — the cached history has to be re-read).
        It carries none of the picker's footer, so a fix that only watches for the
        footer to vanish will call the switch done while the session sits on it.
        Escape backs out of it to the picker, not to the prompt.
        """
        names = (names or ["Default", "Opus", "Fable", "Sonnet", "Haiku"])[:rows]
        state = {"cursor": cursor_start, "screen": "picker", "confirm_cursor": confirm_cursor}

        def capture(pane, scrollback=0):
            if state["screen"] == "closed":
                return {"ok": True, "text": "❯ \n"}
            if state["screen"] == "confirm":
                lines = [
                    "  Switch model?",
                    "  This conversation is cached for the current model. Switching to Fable 5",
                    "  means the full history gets re-read on your next message.",
                ]
                for i, name in enumerate(["Yes, switch to Fable 5", "No, go back"], 1):
                    mark = "❯ " if i == state["confirm_cursor"] else "  "
                    lines.append(f"  {mark}{i}. {name}")
                return {"ok": True, "text": "\n".join(lines) + "\n"}
            span = min(window or rows, rows)
            lo = min(max(1, state["cursor"] - span // 2), rows - span + 1)
            hi = lo + span - 1
            lines = ["  Select model"]
            for i in range(lo, hi + 1):
                if i == state["cursor"]:
                    mark = "❯ "
                elif i == lo and lo > 1:
                    mark = "↑ "
                elif i == hi and hi < rows:
                    mark = "↓ "
                else:
                    mark = "  "
                lines.append(f"  {mark}{i}. {names[i - 1]}   blurb")
            if span < rows:
                lines.append(f"     … +{rows - span} models")
            lines.append("  Enter to set as default · s to use this session only · Esc to cancel")
            return {"ok": True, "text": "\n".join(lines) + "\n"}

        sent = []

        def send_keys(pane, *keys):
            sent.extend(keys)
            for k in keys:
                if state["screen"] == "confirm":
                    if k == "Down":
                        state["confirm_cursor"] = state["confirm_cursor"] % 2 + 1
                    elif k == "Enter":
                        if state["confirm_cursor"] == 1 and not confirm_sticks:
                            state["screen"] = "closed"
                    elif k == "Escape":
                        state["screen"] = "picker"
                elif state["screen"] == "picker":
                    if k == "Down":
                        state["cursor"] = state["cursor"] % rows + 1
                    elif k == "s":
                        state["screen"] = "confirm" if confirm else "closed"
                    elif k == "Escape":
                        state["screen"] = "closed"
            return {"ok": True}

        with mock.patch.object(actions, "find_window", return_value=_fake_window("/dev/pts/9")), \
             mock.patch.object(actions, "send_prompt", return_value={"ok": True}), \
             mock.patch.object(actions.tmux, "pane_for_tty", return_value="%1"), \
             mock.patch.object(actions.tmux, "capture_pane", side_effect=capture), \
             mock.patch.object(actions.tmux, "send_keys", side_effect=send_keys), \
             mock.patch.object(actions.time, "sleep"):
            r = actions.switch_model(1234, alias)
        return r, sent, state

    def test_commits_with_s_not_enter(self):
        r, sent, _ = self._drive("fable")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["model"], "fable")
        self.assertEqual(sent[-1], "s")
        self.assertNotIn("Enter", sent)

    def test_wraps_around_to_reach_target(self):
        # Cursor starts on Sonnet (4); Fable (3) is reachable only by wrapping.
        # Two laps: 5 Downs to survey the list back to where it started, then 4
        # more to step onto Fable.
        _, sent, state = self._drive("fable", cursor_start=4)
        self.assertEqual(sent.count("Down"), 5 + 4)
        self.assertEqual(state["cursor"], 3)

    def test_survey_leaves_the_cursor_where_it_found_it(self):
        # Already on Sonnet: the survey lap still runs — it is the only way to
        # read the folded rows — but it comes back around and commits in place.
        _, sent, state = self._drive("sonnet", cursor_start=4)
        self.assertEqual(sent, ["Down"] * 5 + ["s"])
        self.assertEqual(state["cursor"], 4)

    def test_unknown_alias_escapes_the_dialog(self):
        r, sent, _ = self._drive("gpt5")
        self.assertFalse(r["ok"])
        self.assertIn("not in the /model dialog", r["error"])
        self.assertEqual(sent, ["Down"] * 5 + ["Escape"])
        self.assertNotIn("s", sent)

    def test_reaches_a_model_folded_out_of_view(self):
        # The regression: in an 80x24 pane the picker draws two rows and folds the
        # rest, so Fable is nowhere on the capture the dialog opens with. Reading
        # the target off that one capture is what reported Fable as not offered.
        r, sent, state = self._drive("fable", cursor_start=1, window=2)
        self.assertTrue(r["ok"], r)
        self.assertEqual(state["cursor"], 3)
        self.assertEqual(sent[-1], "s")

    def test_offered_list_names_the_folded_rows_too(self):
        # A model that really isn't there has to say so against the whole list,
        # not against the two rows that happened to be on screen.
        r, _, _ = self._drive("gpt5", cursor_start=1, window=2)
        self.assertFalse(r["ok"])
        for name in ("Default", "Opus", "Fable", "Sonnet", "Haiku"):
            self.assertIn(name, r["error"])

    def test_opus_lands_on_the_bare_row_not_the_1m_one(self):
        # A session on plain Opus 5 gets its own row on top of "Opus (1M
        # context)". Matching on first words alone would take the 1M row.
        _, _, state = self._drive(
            "opus", cursor_start=3, rows=6,
            names=["Default (recommended)", "Opus (1M context)", "Fable",
                   "Sonnet", "Haiku", "Opus ✔"])
        self.assertEqual(state["cursor"], 6)

    def test_confirms_the_cached_history_dialog(self):
        # Mid-conversation, "s" raises a second dialog instead of closing. The
        # switch isn't real until that one is answered Yes.
        r, sent, state = self._drive("fable", confirm=True)
        self.assertTrue(r["ok"], r)
        self.assertEqual(state["screen"], "closed")
        self.assertEqual(sent[-2:], ["s", "Enter"])

    def test_confirm_enter_lands_on_yes_not_whatever_is_highlighted(self):
        # Cursor parked on "No, go back": Enter must not be pressed until it's moved.
        r, sent, state = self._drive("fable", confirm=True, confirm_cursor=2)
        self.assertTrue(r["ok"], r)
        self.assertEqual(state["screen"], "closed")
        self.assertEqual(sent[-3:], ["s", "Down", "Enter"])

    def test_confirm_that_never_closes_escapes_out_of_both_dialogs(self):
        # time.sleep is mocked out, so shorten the wait or the give-up path spins
        # for the full real-time deadline.
        with mock.patch.object(actions, "_MODEL_DIALOG_WAIT", 0.05):
            r, sent, state = self._drive("fable", confirm=True, confirm_sticks=True)
        self.assertFalse(r["ok"])
        self.assertIn("confirm", r["error"].lower())
        # One Escape only backs out to the picker — the session is left on an open
        # modal unless we keep going until the pane is clean.
        self.assertEqual(state["screen"], "closed")

    def test_codex_is_rejected(self):
        with mock.patch.object(actions, "find_window",
                               return_value=_fake_window("/dev/pts/9", platform="codex")):
            r = actions.switch_model(1234, "opus")
        self.assertFalse(r["ok"])
        self.assertIn("Claude-only", r["error"])


# Claude's first-run folder-trust prompt (v2.1.x), as captured from a pane. Its
# options carry no numbers and the cursor opens on "No, exit" — the two facts
# that make it unanswerable by the numbered-permission path.
_TRUST_PROMPT = (
    "\u2500" * 40 + "\n"
    " Accessing workspace:\n"
    " /tmp/proj\n"
    " Quick safety check: Is this a project you created or one you trust? (Like your\n"
    " own code, a well-known open source project, or work from your team). If not,\n"
    " take a moment to review what's in this folder first.\n"
    " Claude Code'll be able to read, edit, and execute files here.\n"
    " Security guide\n"
    " \u276f No, exit\n"
    "   Yes, I trust this folder\n"
    " Enter to confirm \u00b7 Esc to cancel"
)
# The same dialog after one Down: the cursor has moved onto the Yes row.
_TRUST_PROMPT_ON_YES = _TRUST_PROMPT.replace(
    " \u276f No, exit\n   Yes", "   No, exit\n \u276f Yes")


def _keys_recorder(sent):
    """A send_keys stub that records the keys and answers like the real one."""
    def fake(pane, *keys):
        sent.append(keys)
        return {"ok": True}
    return fake


class TrustPromptTests(unittest.TestCase):
    """The folder-trust prompt: recognizing it, and answering it by its cursor.

    Every other dialog the board answers is a numbered list, so "approve" is the
    digit 1. This one has no numbers — the digit lands on nothing and the card
    stays waiting — and Enter commits whatever the cursor is on, which starts on
    "No, exit". Both halves are load-bearing: reach the Yes row, and never press
    Enter without having read the cursor onto it.
    """

    def test_recognizes_the_prompt(self):
        self.assertTrue(actions.trust_prompt_up(_TRUST_PROMPT))
        self.assertTrue(actions.trust_prompt_up(_TRUST_PROMPT_ON_YES))

    def test_other_dialogs_are_not_the_trust_prompt(self):
        self.assertFalse(actions.trust_prompt_up(_PICKER_TEXT))
        self.assertFalse(actions.trust_prompt_up(_LIVE_TEXT))
        # One label alone is a session that printed the words, not the dialog.
        self.assertFalse(actions.trust_prompt_up("I said: Yes, I trust this folder"))

    def test_cursor_label_reads_the_selected_option(self):
        self.assertEqual(actions._trust_cursor_label(_TRUST_PROMPT), "No, exit")
        self.assertEqual(actions._trust_cursor_label(_TRUST_PROMPT_ON_YES),
                         "Yes, I trust this folder")

    def test_cursor_label_ignores_the_composer_prompt(self):
        # The composer and every queued message draw "\u276f " too; neither is an
        # option, and mistaking one for the cursor would commit the wrong row.
        self.assertEqual(actions._trust_cursor_label(_LIVE_TEXT), "")
        self.assertEqual(actions._trust_cursor_label("\u276f do the thing"), "")

    def test_steps_onto_yes_then_confirms(self):
        # Cursor on No, then on Yes after the Down, then the dialog is gone.
        caps = [_TRUST_PROMPT, _TRUST_PROMPT_ON_YES, _LIVE_TEXT]

        def fake_capture(pane, **kw):
            return {"ok": True, "text": caps.pop(0) if caps else _LIVE_TEXT}

        sent = []
        with mock.patch.object(actions.tmux, "capture_pane", side_effect=fake_capture), \
             mock.patch.object(actions.tmux, "send_keys", side_effect=_keys_recorder(sent)), \
             mock.patch.object(actions.time, "sleep"):
            r = actions.answer_trust_prompt("%0")
        self.assertTrue(r["ok"])
        self.assertTrue(r["answered"])
        self.assertEqual(sent, [("Down",), ("Enter",)])

    def test_never_confirms_a_cursor_it_could_not_move(self):
        # A dialog whose cursor never reaches Yes must be left alone: Enter here
        # would commit "No, exit" and kill the session.
        with mock.patch.object(actions.tmux, "capture_pane",
                               return_value={"ok": True, "text": _TRUST_PROMPT}), \
             mock.patch.object(actions.tmux, "send_keys") as sk, \
             mock.patch.object(actions.time, "sleep"):
            r = actions.answer_trust_prompt("%0")
        self.assertFalse(r["ok"])
        self.assertNotIn(("Enter",), [c.args[1:] for c in sk.call_args_list])

    def test_unreadable_pane_sends_nothing(self):
        with mock.patch.object(actions.tmux, "capture_pane", return_value={"ok": False}), \
             mock.patch.object(actions.tmux, "send_keys") as sk, \
             mock.patch.object(actions.time, "sleep"):
            r = actions.answer_trust_prompt("%0")
        self.assertFalse(r["ok"])
        sk.assert_not_called()

    def test_already_answered_prompt_is_a_no_op(self):
        with mock.patch.object(actions.tmux, "capture_pane",
                               return_value={"ok": True, "text": _LIVE_TEXT}), \
             mock.patch.object(actions.tmux, "send_keys") as sk, \
             mock.patch.object(actions.time, "sleep"):
            r = actions.answer_trust_prompt("%0")
        self.assertTrue(r["ok"])
        self.assertFalse(r["answered"])
        sk.assert_not_called()


class RespondPermissionRoutingTests(unittest.TestCase):
    """Which dialog is on screen decides how "approve" is delivered."""

    def _respond(self, pane_text, choice="approve"):
        sent = []
        with mock.patch.object(actions, "find_window",
                               return_value=_fake_window("/dev/pts/9")), \
             mock.patch.object(actions.tmux, "pane_for_tty", return_value="%0"), \
             mock.patch.object(actions.tmux, "capture_pane",
                               return_value={"ok": True, "text": pane_text}), \
             mock.patch.object(actions.tmux, "send_keys", side_effect=_keys_recorder(sent)), \
             mock.patch.object(actions.time, "sleep"):
            r = actions.respond_permission(100, choice)
        return r, sent

    def test_numbered_permission_still_gets_the_digit(self):
        r, sent = self._respond("Do you want to proceed?\n\u276f 1. Yes\n  2. No")
        self.assertTrue(r["ok"])
        self.assertEqual(sent, [("1",)])

    def test_trust_prompt_is_not_answered_with_a_digit(self):
        # The bug this guards: "1" is swallowed by the trust dialog, so the card
        # went on reading "waiting for your input" no matter how often it was
        # clicked. Here the pane never leaves the prompt, so the attempt fails —
        # what matters is that it never sent a digit that does nothing.
        r, sent = self._respond(_TRUST_PROMPT)
        self.assertFalse(r["ok"])
        self.assertNotIn(("1",), sent)

    def test_deny_is_escape_on_the_trust_prompt(self):
        r, sent = self._respond(_TRUST_PROMPT, choice="deny")
        self.assertEqual(sent, [("Escape",)])


class PaneDialogTests(unittest.TestCase):
    """One capture, both facts the snapshot needs about an open dialog."""

    def _run(self, text):
        with mock.patch.object(actions.tmux, "pane_for_tty", return_value="%9"), \
             mock.patch.object(actions.tmux, "capture_pane",
                               return_value={"ok": True, "text": text}):
            return actions.pane_dialog("/dev/pts/9")

    def test_trust_prompt_is_a_menu_and_names_itself(self):
        self.assertEqual(self._run(_TRUST_PROMPT), {"menu": True, "trust": True})

    def test_other_picker_is_a_menu_but_not_trust(self):
        self.assertEqual(self._run(_PICKER_TEXT), {"menu": True, "trust": False})

    def test_live_pane_has_no_dialog(self):
        self.assertEqual(self._run(_LIVE_TEXT), {"menu": False, "trust": False})

    def test_unreadable_pane_is_unknown(self):
        self.assertIsNone(actions.pane_dialog(None))


class ConfirmTrustPromptTests(unittest.TestCase):
    """Answering the folder-trust prompt on a pane that was just launched.

    The pane is empty for the first moment, then shows either the prompt or a
    live composer. Both endings have to be cheap: a spawn into an already
    trusted directory must not sit through the whole attempt budget.
    """

    def _run(self, caps, default=_LIVE_TEXT, **kw):
        seq = list(caps)

        def fake_capture(pane, **_kw):
            return {"ok": True, "text": seq.pop(0) if seq else default}

        sent = []
        with mock.patch.object(actions.tmux, "capture_pane", side_effect=fake_capture), \
             mock.patch.object(actions.tmux, "send_keys", side_effect=_keys_recorder(sent)), \
             mock.patch.object(actions.time, "sleep"):
            return actions.confirm_trust_prompt("%0", **kw), sent

    def test_answers_the_prompt_once_it_paints(self):
        # Two empty polls while Claude boots, then the dialog: cursor on "No,
        # exit" as it opens, on Yes after the Down, gone after the Enter.
        r, sent = self._run(["", "", _TRUST_PROMPT,
                             _TRUST_PROMPT, _TRUST_PROMPT_ON_YES, _LIVE_TEXT])
        self.assertTrue(r["answered"])
        self.assertEqual(r["reason"], "")
        self.assertEqual(sent, [("Down",), ("Enter",)])

    def test_prompt_is_tested_before_the_composer_marker(self):
        # The dialog's cursor row draws "❯" too. Reading that as a live composer
        # would walk away from the very prompt this exists to answer.
        r, sent = self._run([_TRUST_PROMPT, _TRUST_PROMPT_ON_YES, _LIVE_TEXT])
        self.assertTrue(r["answered"])
        self.assertNotEqual(r["reason"], "already trusted")

    def test_settles_before_the_first_keypress(self):
        # Keys sent the instant the dialog paints are swallowed by a TUI that
        # has not armed its input handler yet — and the session exits on its own
        # some moments later. Nothing may be sent before that settle.
        order = []

        # Cursor already on Yes, and the dialog clears once the key lands — so
        # the run ends at the Enter instead of sitting out the confirm wait.
        seq = [_TRUST_PROMPT_ON_YES, _TRUST_PROMPT_ON_YES]

        def fake_capture(pane, **_kw):
            order.append("capture")
            return {"ok": True, "text": seq.pop(0) if seq else _LIVE_TEXT}

        with mock.patch.object(actions.tmux, "capture_pane", side_effect=fake_capture), \
             mock.patch.object(actions.tmux, "send_keys",
                               side_effect=lambda p, *k: (order.append("key"), {"ok": True})[1]), \
             mock.patch.object(actions.time, "sleep",
                               side_effect=lambda s: order.append(f"sleep{s}")):
            actions.confirm_trust_prompt("%0")
        self.assertIn(f"sleep{actions._TRUST_ARM_SETTLE}", order)
        self.assertLess(order.index(f"sleep{actions._TRUST_ARM_SETTLE}"),
                        order.index("key"))

    def test_half_painted_dialog_is_not_mistaken_for_a_composer(self):
        # The cursor row lands on screen a paint before the Yes row. That "❯" is
        # the composer marker; treating it as one here would leave the session
        # parked on the dialog — the exact stuck card this poll exists to stop.
        half = " \u276f No, exit\n"
        r, sent = self._run([half, _TRUST_PROMPT, _TRUST_PROMPT,
                             _TRUST_PROMPT_ON_YES, _LIVE_TEXT])
        self.assertTrue(r["answered"])
        self.assertEqual(sent, [("Down",), ("Enter",)])

    def test_already_trusted_directory_returns_immediately(self):
        r, sent = self._run([_LIVE_TEXT])
        self.assertFalse(r["answered"])
        self.assertEqual(r["reason"], "already trusted")
        self.assertEqual(sent, [])

    def test_budget_runs_out_without_sending_keys(self):
        # A pane that never paints anything: give up, quietly.
        r, sent = self._run([], default="", attempts=2)
        self.assertFalse(r["answered"])
        self.assertEqual(r["reason"], "no prompt")
        self.assertEqual(sent, [])

    def test_no_pane_is_a_no_op(self):
        self.assertEqual(actions.confirm_trust_prompt("")["reason"], "no pane")

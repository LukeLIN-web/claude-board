"""Tests for the two new tmux routes and the tmux_available snapshot flag.

httpx/TestClient is not a project dependency, so we exercise the route handler
functions directly (FastAPI's decorator returns the original function) and the
Pydantic request models at the model level.
"""
import unittest
from unittest import mock

import pydantic

import app as appmod


class RequestModelTests(unittest.TestCase):
    def test_create_body_requires_cwd(self):
        with self.assertRaises(pydantic.ValidationError):
            appmod.CreateBody()

    def test_prompt_body_requires_text(self):
        with self.assertRaises(pydantic.ValidationError):
            appmod.PromptBody()


class CreateRouteTests(unittest.TestCase):
    def test_dispatches_to_create_session(self):
        with mock.patch.object(appmod.actions, "create_session", return_value={"ok": True, "pane_id": "%1"}) as m:
            r = appmod.api_window_create(appmod.CreateBody(cwd="/tmp"))
        m.assert_called_once_with("/tmp")
        self.assertTrue(r["ok"])


class PromptRouteTests(unittest.TestCase):
    def test_dispatches_to_send_prompt(self):
        with mock.patch.object(appmod.actions, "send_prompt", return_value={"ok": True}) as m:
            r = appmod.api_window_prompt(4321, appmod.PromptBody(text="hi there"))
        m.assert_called_once_with(4321, "hi there")
        self.assertTrue(r["ok"])


class SnapshotFlagTests(unittest.TestCase):
    def test_tmux_available_present_with_zero_windows(self):
        empty = {"windows": [], "counts": {}, "ts": 0}
        with mock.patch.object(appmod.sessions, "snapshot", return_value=empty), \
             mock.patch.object(appmod.tmux, "available", return_value=True):
            snap = appmod._enriched_snapshot()
        self.assertIn("tmux_available", snap)
        self.assertTrue(snap["tmux_available"])

    def test_tmux_available_reflects_false(self):
        empty = {"windows": [], "counts": {}, "ts": 0}
        with mock.patch.object(appmod.sessions, "snapshot", return_value=empty), \
             mock.patch.object(appmod.tmux, "available", return_value=False):
            snap = appmod._enriched_snapshot()
        self.assertFalse(snap["tmux_available"])


class HiddenAgentQueueTests(unittest.TestCase):
    """`.slock` agent sub-sessions never write a `status` field (it normalizes to
    "unknown"), yet their pid+tty still back the queue. The Queued list must
    render for them, not only for windows that report `status == "busy"`."""

    def _run(self, win):
        snap = {"windows": [win], "counts": {}, "ts": 0}
        with mock.patch.object(appmod.sessions, "snapshot", return_value=snap), \
             mock.patch.object(appmod.tmux, "available", return_value=True), \
             mock.patch.object(appmod.sessions, "shell_descendant_counts", return_value={}), \
             mock.patch.object(appmod.perms, "pending_by_tty", return_value={}), \
             mock.patch.object(appmod.patrol, "classify",
                               return_value={"triage": "", "reason": "", "suggestion": ""}), \
             mock.patch.object(appmod.promptqueue, "pending",
                               return_value=[{"id": 1, "text": "/btw"}]), \
             mock.patch.object(appmod.actions, "get_pane_queue", return_value=[]):
            return appmod._enriched_snapshot()

    def test_queue_renders_for_hidden_agent_without_busy_status(self):
        win = {"pid": 4163977, "status": "unknown", "hidden": True, "alive": True,
               "tty": "pts/14", "transcript_path": None, "name": "agent", "cwd": "/x",
               "updated_at": 0}
        out = self._run(win)
        self.assertEqual(out["windows"][0]["queued"],
                         [{"text": "/btw", "source": "dashboard"}])

    def test_dead_hidden_agent_has_no_queue(self):
        win = {"pid": 4163977, "status": "unknown", "hidden": True, "alive": False,
               "tty": None, "transcript_path": None, "name": "agent", "cwd": "/x",
               "updated_at": 0}
        out = self._run(win)
        self.assertEqual(out["windows"][0]["queued"], [])


class DiffSignatureTests(unittest.TestCase):
    """The SSE watcher only broadcasts when `diff_signature` changes. A queued
    prompt being consumed (or added) while status/updated_at stay the same must
    still change the signature, or the card keeps showing a stale queue."""

    def _win(self, queued):
        return {"pid": 100, "status": "busy", "waiting_for": None,
                "updated_at": 5, "queued": queued}

    def test_queue_change_alone_changes_signature(self):
        st = appmod.State()
        before = {"windows": [self._win(
            [{"text": "/btw", "source": "dashboard"}])], "counts": {}, "ts": 0}
        after = {"windows": [self._win([])], "counts": {}, "ts": 0}
        self.assertNotEqual(
            st.diff_signature(before), st.diff_signature(after),
            "consuming a queued prompt must change the broadcast signature")


if __name__ == "__main__":
    unittest.main()

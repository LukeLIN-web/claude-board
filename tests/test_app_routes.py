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


if __name__ == "__main__":
    unittest.main()

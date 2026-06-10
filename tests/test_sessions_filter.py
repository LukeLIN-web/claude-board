"""Tests for the machine-local cwd visibility filter in core/sessions.py."""
import unittest
from unittest import mock

from core import sessions


class CwdFilterTests(unittest.TestCase):
    def tearDown(self):
        # Restore default (no filtering) so other tests are unaffected.
        with mock.patch.dict("os.environ", {}, clear=False):
            sessions._reload_cwd_filters()

    def _set(self, **env):
        with mock.patch.dict("os.environ", env, clear=False):
            sessions._reload_cwd_filters()

    def test_no_env_shows_everything(self):
        sessions._CWD_INCLUDE, sessions._CWD_EXCLUDE = [], []
        self.assertTrue(sessions._cwd_visible("/home/user1/workspace/x"))
        self.assertTrue(sessions._cwd_visible("/anything"))

    def test_include_allowlist(self):
        self._set(CLAUDE_FLEET_CWD_INCLUDE="/shared/user60/workspace/juyi/")
        self.assertTrue(sessions._cwd_visible("/shared/user60/workspace/juyi/board"))
        self.assertTrue(sessions._cwd_visible("/shared/user60/workspace/juyi"))
        self.assertFalse(sessions._cwd_visible("/home/user1/workspace/x"))

    def test_include_respects_path_boundary(self):
        self._set(CLAUDE_FLEET_CWD_INCLUDE="/shared/user60/workspace/juyi")
        # A sibling dir that merely shares the prefix string must not match.
        self.assertFalse(sessions._cwd_visible("/shared/user60/workspace/juyi-evil"))

    def test_exclude_denylist(self):
        self._set(CLAUDE_FLEET_CWD_EXCLUDE="/home/user1/workspace")
        self.assertFalse(sessions._cwd_visible("/home/user1/workspace/x"))
        self.assertTrue(sessions._cwd_visible("/shared/user60/workspace/juyi/board"))

    def test_exclude_wins_over_include(self):
        self._set(
            CLAUDE_FLEET_CWD_INCLUDE="/shared",
            CLAUDE_FLEET_CWD_EXCLUDE="/shared/user60/secret",
        )
        self.assertTrue(sessions._cwd_visible("/shared/user60/workspace/juyi"))
        self.assertFalse(sessions._cwd_visible("/shared/user60/secret/x"))

    def test_multiple_prefixes(self):
        self._set(CLAUDE_FLEET_CWD_INCLUDE="/a/b:/c/d,/e/f")
        for p in ("/a/b/x", "/c/d/y", "/e/f/z"):
            self.assertTrue(sessions._cwd_visible(p))
        self.assertFalse(sessions._cwd_visible("/g/h"))


class SlugFilterTests(unittest.TestCase):
    def tearDown(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            sessions._reload_cwd_filters()

    def test_slug_matches_cwd_filter(self):
        with mock.patch.dict(
            "os.environ",
            {"CLAUDE_FLEET_CWD_INCLUDE": "/shared/user60/workspace/juyi"},
            clear=False,
        ):
            sessions._reload_cwd_filters()
        # slug form of an allowed cwd is visible...
        self.assertTrue(sessions.slug_visible("-shared-user60-workspace-juyi-board"))
        # ...a sibling sharing the string prefix is not (boundary on "-")...
        self.assertFalse(sessions.slug_visible("-shared-user60-workspace-juyi2-x"))
        # ...and an unrelated project is hidden.
        self.assertFalse(sessions.slug_visible("-home-user1-arman-lingbot-va"))


class HistoryFilterTests(unittest.TestCase):
    """history.list_sessions must drop sessions whose project is hidden."""

    def tearDown(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            sessions._reload_cwd_filters()

    def test_list_sessions_drops_hidden_projects(self):
        from core import history

        def mk(sid, project):
            return history.HistorySession(
                session_id=sid, project=project, project_name=project.rsplit("/", 1)[-1],
                first_input="", input_count=0, first_ts="", last_ts="",
                transcript_path=None, transcript_size=0, transcript_mtime=0,
                is_alive=False,
            )

        fake = [
            mk("a", "/shared/user60/workspace/juyi/board"),
            mk("b", "/home/user1/arman/lingbot-va"),
        ]
        with mock.patch.dict(
            "os.environ",
            {"CLAUDE_FLEET_CWD_INCLUDE": "/shared/user60/workspace/juyi"},
            clear=False,
        ):
            sessions._reload_cwd_filters()
            with mock.patch.object(history, "_build_index", return_value=fake), \
                 mock.patch.object(history, "_cache", []), \
                 mock.patch.object(history, "_cache_ts", 0):
                out = history.list_sessions(limit=9999)
        sids = {s["session_id"] for s in out["sessions"]}
        self.assertEqual(sids, {"a"})
        self.assertEqual(out["total"], 1)


if __name__ == "__main__":
    unittest.main()

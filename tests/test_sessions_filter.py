"""Tests for the machine-local cwd visibility filter in core/sessions.py."""
import unittest
from unittest import mock

from core import sessions


def _load_filters(include: str = "", exclude: str = "") -> None:
    """Load the cwd filter from exactly these two values, nothing ambient.

    Both vars are always patched, and the default is "" — no filtering. Setting
    only the one a test is about (with `clear=False`) is not enough: run.sh
    exports CLAUDE_FLEET_CWD_INCLUDE out of .env.local, and the Claude sessions
    the board spawns inherit it — which is where these tests get run. A test that
    set only EXCLUDE then ran against the board's own allowlist and failed on the
    machine the board was running on.
    """
    with mock.patch.dict("os.environ",
                         {"CLAUDE_FLEET_CWD_INCLUDE": include,
                          "CLAUDE_FLEET_CWD_EXCLUDE": exclude},
                         clear=False):
        sessions._reload_cwd_filters()


class CwdFilterTests(unittest.TestCase):
    def tearDown(self):
        _load_filters()  # no filtering, so other tests are unaffected

    def test_no_env_shows_everything(self):
        _load_filters()
        self.assertTrue(sessions._cwd_visible("/home/user1/workspace/x"))
        self.assertTrue(sessions._cwd_visible("/anything"))

    def test_include_allowlist(self):
        _load_filters(include="/shared/ws/proj/")
        self.assertTrue(sessions._cwd_visible("/shared/ws/proj/board"))
        self.assertTrue(sessions._cwd_visible("/shared/ws/proj"))
        self.assertFalse(sessions._cwd_visible("/home/user1/workspace/x"))

    def test_include_respects_path_boundary(self):
        _load_filters(include="/shared/ws/proj")
        # A sibling dir that merely shares the prefix string must not match.
        self.assertFalse(sessions._cwd_visible("/shared/ws/proj-evil"))

    def test_exclude_denylist(self):
        _load_filters(exclude="/home/user1/workspace")
        self.assertFalse(sessions._cwd_visible("/home/user1/workspace/x"))
        self.assertTrue(sessions._cwd_visible("/shared/ws/proj/board"))

    def test_exclude_wins_over_include(self):
        _load_filters(include="/shared", exclude="/shared/ws/secret")
        self.assertTrue(sessions._cwd_visible("/shared/ws/proj"))
        self.assertFalse(sessions._cwd_visible("/shared/ws/secret/x"))

    def test_multiple_prefixes(self):
        _load_filters(include="/a/b:/c/d,/e/f")
        for p in ("/a/b/x", "/c/d/y", "/e/f/z"):
            self.assertTrue(sessions._cwd_visible(p))
        self.assertFalse(sessions._cwd_visible("/g/h"))


class SlugFilterTests(unittest.TestCase):
    def tearDown(self):
        _load_filters()

    def test_slug_matches_cwd_filter(self):
        _load_filters(include="/shared/ws/proj")
        # slug form of an allowed cwd is visible...
        self.assertTrue(sessions.slug_visible("-shared-ws-proj-board"))
        # ...a sibling sharing the string prefix is not (boundary on "-")...
        self.assertFalse(sessions.slug_visible("-shared-ws-proj2-x"))
        # ...and an unrelated project is hidden.
        self.assertFalse(sessions.slug_visible("-home-user1-arman-lingbot-va"))


class HistoryFilterTests(unittest.TestCase):
    """history.list_sessions must drop sessions whose project is hidden."""

    def tearDown(self):
        _load_filters()

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
            mk("a", "/shared/ws/proj/board"),
            mk("b", "/home/user1/arman/lingbot-va"),
        ]
        _load_filters(include="/shared/ws/proj")
        with mock.patch.object(history, "_build_index", return_value=fake), \
             mock.patch.object(history, "_cache", []), \
             mock.patch.object(history, "_cache_ts", 0):
            out = history.list_sessions(limit=9999)
        sids = {s["session_id"] for s in out["sessions"]}
        self.assertEqual(sids, {"a"})
        self.assertEqual(out["total"], 1)


class UninterruptibleWrappersTests(unittest.TestCase):
    """uninterruptible_wrappers finds exactly the wedged Bash-tool wrappers a
    force-kill should target: a direct SHELL child of the claude pid whose
    subtree holds a D-state process."""

    CLAUDE = 100

    def _run(self, rows):
        with mock.patch.object(sessions, "_proc_snapshot", return_value=rows):
            return sessions.uninterruptible_wrappers(self.CLAUDE)

    def test_bash_wrapper_with_d_grandchild_is_found(self):
        # claude(100) -> bash(200,S) -> nvidia-smi(300,D)
        rows = [
            (self.CLAUDE, 1, "Ssl+", "claude"),
            (200, self.CLAUDE, "Ss", "bash"),
            (300, 200, "Dl", "nvidia-smi"),
        ]
        self.assertEqual(self._run(rows), [200])

    def test_bash_wrapper_without_d_descendant_is_left_alone(self):
        # A normal, interruptible command — Esc can handle it, don't force-kill.
        rows = [
            (self.CLAUDE, 1, "Ssl+", "claude"),
            (200, self.CLAUDE, "Ss", "bash"),
            (300, 200, "S", "grep"),
        ]
        self.assertEqual(self._run(rows), [])

    def test_non_shell_child_with_d_descendant_is_not_targeted(self):
        # e.g. the codex mcp-server (node) — never force-kill it.
        rows = [
            (self.CLAUDE, 1, "Ssl+", "claude"),
            (400, self.CLAUDE, "Sl+", "node"),
            (401, 400, "D", "something"),
        ]
        self.assertEqual(self._run(rows), [])

    def test_d_process_nested_below_an_intermediate_shell(self):
        # claude(100) -> bash(200) -> sh(250) -> proc(300,D): the DIRECT child
        # 200 is the reap target Claude awaits.
        rows = [
            (self.CLAUDE, 1, "Ssl+", "claude"),
            (200, self.CLAUDE, "Ss", "bash"),
            (250, 200, "S", "sh"),
            (300, 250, "D", "nvidia-smi"),
        ]
        self.assertEqual(self._run(rows), [200])

    def test_multiple_wrappers_only_the_wedged_ones(self):
        rows = [
            (self.CLAUDE, 1, "Ssl+", "claude"),
            (200, self.CLAUDE, "Ss", "bash"),      # wedged
            (300, 200, "Dl", "nvidia-smi"),
            (210, self.CLAUDE, "Ss", "bash"),      # healthy
            (310, 210, "S", "tail"),
        ]
        self.assertEqual(self._run(rows), [200])

    def test_empty_snapshot_returns_empty(self):
        self.assertEqual(self._run([]), [])

    def test_no_children_returns_empty(self):
        self.assertEqual(self._run([(self.CLAUDE, 1, "Ssl+", "claude")]), [])


if __name__ == "__main__":
    unittest.main()

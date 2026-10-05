"""Tests for the machine-local cwd visibility filter in core/sessions.py."""
import unittest
from unittest import mock

from core import sessions
from tests.helpers import queue_op, scratch_dir, user_row, write_jsonl


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
        self.assertTrue(sessions._cwd_visible("/home/u/workspace/x"))
        self.assertTrue(sessions._cwd_visible("/anything"))

    def test_include_allowlist(self):
        _load_filters(include="/shared/ws/proj/")
        self.assertTrue(sessions._cwd_visible("/shared/ws/proj/board"))
        self.assertTrue(sessions._cwd_visible("/shared/ws/proj"))
        self.assertFalse(sessions._cwd_visible("/home/u/workspace/x"))

    def test_include_respects_path_boundary(self):
        _load_filters(include="/shared/ws/proj")
        # A sibling dir that merely shares the prefix string must not match.
        self.assertFalse(sessions._cwd_visible("/shared/ws/proj-evil"))

    def test_exclude_denylist(self):
        _load_filters(exclude="/home/u/workspace")
        self.assertFalse(sessions._cwd_visible("/home/u/workspace/x"))
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

    def test_root_prefix_covers_everything(self):
        # normpath("/") is "/", and "/" + os.sep matched nothing at all.
        _load_filters(include="/")
        self.assertTrue(sessions._cwd_visible("/home/u/workspace/x"))
        self.assertTrue(sessions.slug_visible("-home-u-workspace-x"))
        _load_filters(exclude="/")
        self.assertFalse(sessions._cwd_visible("/home/u/workspace/x"))
        self.assertFalse(sessions.slug_visible("-home-u-workspace-x"))


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
        self.assertFalse(sessions.slug_visible("-home-u-other-lingbot-va"))


class TranscriptFilterTests(unittest.TestCase):
    """transcript_visible: the filter for a transcript, on the cwd it records."""

    def setUp(self):
        self.root = scratch_dir()

    def tearDown(self):
        _load_filters()

    def _transcript(self, slug, cwd):
        """A transcript in projects/<slug>/ whose rows record `cwd` — none
        when it is "" — behind a first row that names no cwd at all."""
        rows = [queue_op("enqueue")] + ([user_row("hi", cwd=cwd)] if cwd else [])
        return write_jsonl(rows, self.root / slug / "s.jsonl")

    def test_a_sibling_that_extends_the_name_is_not_inside(self):
        # The same boundary test_include_respects_path_boundary holds for a cwd.
        # Its slug "-shared-ws-proj-evil" reads as under "-shared-ws-proj".
        _load_filters(include="/shared/ws/proj")
        evil = self._transcript("-shared-ws-proj-evil", "/shared/ws/proj-evil")
        inside = self._transcript("-shared-ws-proj-evil2", "/shared/ws/proj/evil2")
        self.assertTrue(sessions.slug_visible("-shared-ws-proj-evil"))  # the lossy one
        self.assertFalse(sessions.transcript_visible(evil))
        self.assertTrue(sessions.transcript_visible(inside))

    def test_no_cwd_row_falls_back_to_the_slug(self):
        _load_filters(exclude="/home/u/workspace")
        p = self._transcript("-home-u-workspace-x", "")
        self.assertFalse(sessions.transcript_visible(p))

    def test_no_filter_reads_nothing(self):
        _load_filters()
        self.assertTrue(sessions.transcript_visible(self.root / "missing" / "s.jsonl"))


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
            mk("b", "/home/u/other/lingbot-va"),
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
        table = {pid: sessions.Proc(ppid, stat, "?", comm, comm)
                 for pid, ppid, stat, comm in rows}
        with mock.patch.object(sessions, "proc_table", return_value=table):
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

"""Pure-logic tests for core/tmux.py — subprocess.run is always faked."""
import subprocess
import unittest
from unittest import mock

from core import tmux


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_run(side_effect=None, **proc_kwargs):
    """Patch core.tmux.subprocess.run; return the mock."""
    if side_effect is not None:
        return mock.patch.object(tmux.subprocess, "run", side_effect=side_effect)
    return mock.patch.object(tmux.subprocess, "run", return_value=FakeProc(**proc_kwargs))


class RunHelperTests(unittest.TestCase):
    def setUp(self):
        tmux._clear_caches()

    def test_run_returns_structured_schema_on_success(self):
        with _patch_run(returncode=0, stdout="hi", stderr=""):
            r = tmux._run("display-message", "-p", "x")
        self.assertEqual(set(r), {"ok", "rc", "stdout", "stderr", "error"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["rc"], 0)
        self.assertEqual(r["stdout"], "hi")
        self.assertEqual(r["error"], "")

    def test_run_nonzero_exit_sets_error_from_stderr(self):
        with _patch_run(returncode=1, stdout="", stderr="boom"):
            r = tmux._run("list-sessions")
        self.assertFalse(r["ok"])
        self.assertEqual(r["rc"], 1)
        self.assertEqual(r["error"], "boom")

    def test_run_never_raises_on_missing_binary(self):
        with _patch_run(side_effect=FileNotFoundError("tmux")):
            r = tmux._run("list-sessions")
        self.assertFalse(r["ok"])
        self.assertIsNone(r["rc"])
        self.assertTrue(r["error"])  # non-empty message

    def test_run_never_raises_on_timeout(self):
        exc = subprocess.TimeoutExpired(cmd="tmux", timeout=10)
        with _patch_run(side_effect=exc):
            r = tmux._run("list-sessions")
        self.assertFalse(r["ok"])
        self.assertIn("time", r["error"].lower())

    def test_run_invokes_tmux_with_args(self):
        with _patch_run(returncode=0) as m:
            tmux._run("list-panes", "-a")
        argv = m.call_args[0][0]
        self.assertEqual(argv[:3], ["tmux", "list-panes", "-a"])


class AvailableTests(unittest.TestCase):
    def setUp(self):
        tmux._clear_caches()

    def test_available_true_when_tmux_env_set(self):
        with mock.patch.dict("os.environ", {"TMUX": "/tmp/tmux-1/default,123,0"}):
            with _patch_run(returncode=1) as m:  # would fail, but env shortcut wins
                self.assertTrue(tmux.available())
            m.assert_not_called()

    def test_available_true_when_list_sessions_exits_zero(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with _patch_run(returncode=0):
                self.assertTrue(tmux.available())

    def test_available_false_when_tmux_missing(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with _patch_run(side_effect=FileNotFoundError("tmux")):
                self.assertFalse(tmux.available())

    def test_available_is_cached_no_repeated_subprocess(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with _patch_run(returncode=0) as m:
                for _ in range(5):
                    tmux.available()
                self.assertEqual(m.call_count, 1)


class ListPanesTests(unittest.TestCase):
    def setUp(self):
        tmux._clear_caches()

    def test_list_panes_parses_tab_format(self):
        out = "%5\t/dev/pts/3\twork\t/home/u/proj\n%6\t/dev/pts/9\tmain\t/tmp\n"
        with _patch_run(returncode=0, stdout=out) as m:
            panes = tmux.list_panes()
        argv = m.call_args[0][0]
        self.assertIn("list-panes", argv)
        self.assertIn("-a", argv)
        self.assertEqual(len(panes), 2)
        self.assertEqual(
            panes[0],
            {"pane_id": "%5", "tty": "/dev/pts/3", "session": "work", "path": "/home/u/proj"},
        )

    def test_list_panes_returns_empty_on_error(self):
        with _patch_run(side_effect=FileNotFoundError("tmux")):
            self.assertEqual(tmux.list_panes(), [])


class PaneForTtyTests(unittest.TestCase):
    def setUp(self):
        tmux._clear_caches()
        self._out = "%5\t/dev/pts/3\twork\t/home/u/proj\n"

    def test_matches_with_dev_prefix(self):
        with _patch_run(returncode=0, stdout=self._out):
            self.assertEqual(tmux.pane_for_tty("/dev/pts/3"), "%5")

    def test_matches_without_dev_prefix(self):
        with _patch_run(returncode=0, stdout=self._out):
            self.assertEqual(tmux.pane_for_tty("pts/3"), "%5")

    def test_miss_returns_none(self):
        with _patch_run(returncode=0, stdout=self._out):
            self.assertIsNone(tmux.pane_for_tty("pts/99"))

    def test_empty_tty_returns_none(self):
        with _patch_run(returncode=0, stdout=self._out):
            self.assertIsNone(tmux.pane_for_tty(""))
            self.assertIsNone(tmux.pane_for_tty("   "))


class NewWindowTests(unittest.TestCase):
    def setUp(self):
        tmux._clear_caches()

    def test_argv_uses_env_target(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            if "list-sessions" in argv:
                return FakeProc(returncode=0, stdout="mysess\nother\n")
            return FakeProc(returncode=0, stdout="%12\n")

        with mock.patch.dict("os.environ", {"FLEET_TMUX_SESSION": "mysess"}, clear=True):
            with mock.patch.object(tmux.subprocess, "run", side_effect=fake_run):
                r = tmux.new_window("/home/u/proj")
        new_win_argv = [a for a in calls if "new-window" in a][0]
        self.assertEqual(
            new_win_argv,
            ["tmux", "new-window", "-P", "-F", "#{pane_id}",
             "-t", "mysess", "-c", "/home/u/proj",
             "claude", "--dangerously-skip-permissions"],
        )
        self.assertTrue(r["ok"])
        self.assertEqual(r["pane_id"], "%12")

    def test_falls_back_to_first_listed_session(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            if "list-sessions" in argv:
                return FakeProc(returncode=0, stdout="alpha\nbeta\n")
            return FakeProc(returncode=0, stdout="%20\n")

        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch.object(tmux.subprocess, "run", side_effect=fake_run):
                r = tmux.new_window("/tmp")
        new_win_argv = [a for a in calls if "new-window" in a][0]
        self.assertIn("alpha", new_win_argv)
        self.assertTrue(r["ok"])

    def test_no_target_resolvable_returns_error_without_new_window(self):
        def fake_run(argv, **kw):
            if "list-sessions" in argv:
                return FakeProc(returncode=1, stdout="", stderr="no server")
            raise AssertionError("new-window must not be attempted")

        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch.object(tmux.subprocess, "run", side_effect=fake_run):
                r = tmux.new_window("/tmp")
        self.assertFalse(r["ok"])
        self.assertTrue(r["error"])

    def test_new_window_nonzero_exit_returns_error(self):
        def fake_run(argv, **kw):
            if "list-sessions" in argv:
                return FakeProc(returncode=0, stdout="alpha\n")
            return FakeProc(returncode=1, stderr="can't create window")

        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch.object(tmux.subprocess, "run", side_effect=fake_run):
                r = tmux.new_window("/tmp")
        self.assertFalse(r["ok"])
        self.assertIn("create window", r["error"])

    def test_env_target_missing_from_sessions_errors_clearly(self):
        def fake_run(argv, **kw):
            if "list-sessions" in argv:
                return FakeProc(returncode=0, stdout="alpha\nbeta\n")
            raise AssertionError("new-window must not be attempted for a missing target")

        with mock.patch.dict("os.environ", {"FLEET_TMUX_SESSION": "ghost"}, clear=True):
            with mock.patch.object(tmux.subprocess, "run", side_effect=fake_run):
                r = tmux.new_window("/tmp")
        self.assertFalse(r["ok"])
        self.assertIn("ghost", r["error"])


class SendTextTests(unittest.TestCase):
    def setUp(self):
        tmux._clear_caches()

    def test_sends_literal_then_separate_enter(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return FakeProc(returncode=0)

        with mock.patch.object(tmux.subprocess, "run", side_effect=fake_run):
            r = tmux.send_text("%5", "hello world")
        self.assertTrue(r["ok"])
        self.assertEqual(
            calls[0],
            ["tmux", "send-keys", "-t", "%5", "-l", "--", "hello world"],
        )
        self.assertEqual(calls[1], ["tmux", "send-keys", "-t", "%5", "Enter"])

    def test_literal_failure_short_circuits_before_enter(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return FakeProc(returncode=1, stderr="bad pane")

        with mock.patch.object(tmux.subprocess, "run", side_effect=fake_run):
            r = tmux.send_text("%5", "hi")
        self.assertFalse(r["ok"])
        self.assertEqual(len(calls), 1)  # Enter never sent

    def test_slash_prefix_settles_before_enter(self):
        calls = []
        sleeps = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return FakeProc(returncode=0)

        with mock.patch.object(tmux.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(tmux.time, "sleep", side_effect=sleeps.append):
            r = tmux.send_text("%5", "/research-pipeline")
        self.assertTrue(r["ok"])
        self.assertEqual(sleeps, [tmux._SLASH_SETTLE])
        self.assertEqual(calls[1], ["tmux", "send-keys", "-t", "%5", "Enter"])

    def test_plain_text_does_not_sleep(self):
        def fake_run(argv, **kw):
            return FakeProc(returncode=0)

        with mock.patch.object(tmux.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(tmux.time, "sleep") as sl:
            r = tmux.send_text("%5", "research-pipeline")
        self.assertTrue(r["ok"])
        sl.assert_not_called()

    def test_enter_failure_is_reported(self):
        def fake_run(argv, **kw):
            if argv[-1] == "Enter":
                return FakeProc(returncode=1, stderr="enter failed")
            return FakeProc(returncode=0)

        with mock.patch.object(tmux.subprocess, "run", side_effect=fake_run):
            r = tmux.send_text("%5", "hi")
        self.assertFalse(r["ok"])
        self.assertIn("enter failed", r["error"])


if __name__ == "__main__":
    unittest.main()

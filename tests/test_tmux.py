"""Pure-logic tests for core/tmux.py — subprocess.run is always faked."""
import os
import subprocess
import unittest
from unittest import mock

from core import tmux

# Every argv assertion below is about the command being built, so the module runs
# with FLEET_TMUX_SOCKET unset. Leaving it to the ambient environment made these
# tests pass or fail on whether the machine running them had `.env.local`'s
# socket exported — `tmux …` there, `tmux -L board …` here. SocketArgsTests sets
# the variable itself, and covers what it does.
_ambient_socket = None


def setUpModule():
    global _ambient_socket
    env = {k: v for k, v in os.environ.items() if k != "FLEET_TMUX_SOCKET"}
    _ambient_socket = mock.patch.dict("os.environ", env, clear=True)
    _ambient_socket.start()


def tearDownModule():
    _ambient_socket.stop()


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


def _recorder(calls, **proc):
    """A subprocess.run stand-in that appends each argv to `calls` and answers
    every call with the same FakeProc(**proc)."""
    def fake_run(argv, **kw):
        calls.append(argv)
        return FakeProc(**proc)
    return fake_run


def _server(calls=None, *, sessions=("alpha",), pane="%9", panes=None,
            in_mode=False, window_error=None):
    """A fake tmux server for new_window, recording each argv into `calls`.

    `sessions` is what list-sessions shows (none: no server is running yet);
    `pane` is the id a new window or session reports; `panes`, when given, is
    what list-panes prints; `in_mode` opens that pane in view-mode;
    `window_error` makes new-window / new-session fail with that stderr.
    """
    def fake_run(argv, **kw):
        if calls is not None:
            calls.append(argv)
        if "list-sessions" in argv:
            if not sessions:
                return FakeProc(returncode=1, stderr="no server")
            return FakeProc(stdout="".join(f"{s}\n" for s in sessions))
        if "list-panes" in argv and panes is not None:
            return FakeProc(stdout=panes)
        if "#{pane_in_mode}" in argv:
            return FakeProc(stdout="1\n" if in_mode else "0\n")
        if window_error and ("new-window" in argv or "new-session" in argv):
            return FakeProc(returncode=1, stderr=window_error)
        return FakeProc(stdout=f"{pane}\n")
    return fake_run


def _argv(calls, command):
    """The first recorded tmux argv that runs `command`."""
    return next(a for a in calls if command in a)


# Stand-in for the resolved CLI. new_window launches by absolute path, so argv
# assertions would otherwise read whatever `claude` this machine has installed —
# or fail outright on one that has none.
_EXE = "/opt/bin/claude"


def _pin_cli(path=_EXE):
    return mock.patch.object(tmux, "_resolve_cli", return_value=path)


class RunHelperTests(unittest.TestCase):
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


class SocketArgsTests(unittest.TestCase):
    """FLEET_TMUX_SOCKET routes every call to an isolated tmux server."""

    def test_run_injects_socket_before_command(self):
        with mock.patch.dict("os.environ", {"FLEET_TMUX_SOCKET": "board"}, clear=True):
            with _patch_run(returncode=0) as m:
                tmux._run("list-panes", "-a")
        argv = m.call_args[0][0]
        self.assertEqual(argv, ["tmux", "-L", "board", "list-panes", "-a"])

    def test_run_omits_socket_when_unset(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with _patch_run(returncode=0) as m:
                tmux._run("list-panes", "-a")
        argv = m.call_args[0][0]
        self.assertEqual(argv, ["tmux", "list-panes", "-a"])

    def test_run_omits_socket_when_blank(self):
        with mock.patch.dict("os.environ", {"FLEET_TMUX_SOCKET": "  "}, clear=True):
            with _patch_run(returncode=0) as m:
                tmux._run("list-panes")
        argv = m.call_args[0][0]
        self.assertEqual(argv, ["tmux", "list-panes"])

    def test_new_window_targets_socketed_server(self):
        # Socket is dedicated (-L board) but the session is NOT pinned: it falls
        # out of sessions[0] on that server, so cards land wherever that server
        # already hosts, not a hard-coded name.
        calls = []
        with mock.patch.dict("os.environ", {"FLEET_TMUX_SOCKET": "board"}, clear=True), \
             _pin_cli(), mock.patch.object(tmux, "_SPAWN_LANDED_WAITS", (0.0,)), \
             mock.patch.object(tmux, "pane_alive", return_value=True):
            with _patch_run(_server(calls, sessions=["beauty"], pane="%3")):
                r = tmux.new_window("/tmp")
        self.assertTrue(r["ok"])
        self.assertEqual(_argv(calls, "list-sessions")[:3], ["tmux", "-L", "board"])
        new_win_argv = _argv(calls, "new-window")
        self.assertEqual(new_win_argv[:4], ["tmux", "-L", "board", "new-window"])
        self.assertIn("beauty", new_win_argv)  # sessions[0], not a pin


# Only available() and pane_for_tty() read tmux's module caches, so only their
# classes reset them: before each test, and after, so what a test cached never
# reaches the next class.
class AvailableTests(unittest.TestCase):
    def setUp(self):
        tmux._clear_caches()
        self.addCleanup(tmux._clear_caches)

    def test_available_true_when_tmux_env_set(self):
        with mock.patch.dict("os.environ", {"TMUX": "/tmp/tmux-1/default,123,0"}):
            with _patch_run(returncode=1) as m:  # would fail, but env shortcut wins
                self.assertTrue(tmux.available())
            m.assert_not_called()

    def test_available_true_when_start_server_exits_zero(self):
        # start-server succeeds even with zero sessions, so the spawn UI stays
        # available for creating the first session.
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
    def test_list_panes_parses_tab_format(self):
        out = "%5\t/dev/pts/3\n%6\t/dev/pts/9\n"
        with _patch_run(returncode=0, stdout=out) as m:
            panes = tmux.list_panes()
        argv = m.call_args[0][0]
        self.assertIn("list-panes", argv)
        self.assertIn("-a", argv)
        self.assertEqual(len(panes), 2)
        self.assertEqual(panes[0], {"pane_id": "%5", "tty": "/dev/pts/3"})

    def test_list_panes_returns_empty_on_error(self):
        with _patch_run(side_effect=FileNotFoundError("tmux")):
            self.assertEqual(tmux.list_panes(), [])


class PaneForTtyTests(unittest.TestCase):
    def setUp(self):
        tmux._clear_caches()
        self.addCleanup(tmux._clear_caches)
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

    def test_hit_reuses_the_listing_and_a_miss_relists(self):
        # Every card resolves its pane on every poll; a fresh listing each time
        # was a `list-panes` fork per card. A miss must still re-list, or a
        # pane spawned since the last listing would read as missing.
        with _patch_run(returncode=0, stdout=self._out) as m:
            self.assertEqual(tmux.pane_for_tty("/dev/pts/3"), "%5")
            self.assertEqual(tmux.pane_for_tty("pts/3"), "%5")
            self.assertEqual(m.call_count, 1)
            self.assertIsNone(tmux.pane_for_tty("pts/99"))
            self.assertEqual(m.call_count, 2)


class NewWindowTests(unittest.TestCase):
    def setUp(self):
        # Pin what the argv assertions below see. The post-spawn liveness probe
        # is SpawnLandedTests' subject, not theirs — stubbed here so it neither
        # sleeps nor needs every fake to model `list-panes`.
        for patcher in (_pin_cli(),
                        mock.patch.object(tmux, "_SPAWN_LANDED_WAITS", (0.0,)),
                        mock.patch.object(tmux, "pane_alive", return_value=True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_argv_uses_env_target(self):
        calls = []
        # _EXE's dir already on the board's PATH: the pane needs no PATH fix.
        with mock.patch.dict("os.environ", {"FLEET_TMUX_SESSION": "mysess",
                                            "PATH": "/opt/bin"}, clear=True), \
             mock.patch.object(tmux, "_venv_bin_dirs", return_value=set()):
            with _patch_run(_server(calls, sessions=["mysess", "other"], pane="%12")):
                r = tmux.new_window("/home/u/proj")
        self.assertEqual(
            _argv(calls, "new-window"),
            ["tmux", "new-window", "-P", "-F", "#{pane_id}",
             "-t", "mysess", "-c", "/home/u/proj",
             _EXE, "--dangerously-skip-permissions"],
        )
        self.assertTrue(r["ok"])
        self.assertEqual(r["pane_id"], "%12")

    def test_falls_back_to_first_listed_session(self):
        calls = []
        with mock.patch.dict("os.environ", {}, clear=True):
            with _patch_run(_server(calls, sessions=["alpha", "beta"], pane="%20")):
                r = tmux.new_window("/tmp")
        self.assertIn("alpha", _argv(calls, "new-window"))
        self.assertTrue(r["ok"])

    def test_cold_start_creates_session_instead_of_new_window(self):
        # Zero sessions: must bootstrap a host session running cmd directly,
        # not dead-end. new-window has nothing to attach to.
        calls = []
        with mock.patch.dict("os.environ", {"PATH": "/opt/bin"}, clear=True), \
             mock.patch.object(tmux, "_venv_bin_dirs", return_value=set()):
            with _patch_run(_server(calls, sessions=(), pane="%1")):
                r = tmux.new_window("/tmp")
        self.assertTrue(r["ok"])
        self.assertEqual(r["pane_id"], "%1")
        self.assertFalse(any("new-window" in a for a in calls))
        # The server must be started in its own call *before* new-session, so a
        # cold-start new-session only attaches and never forks the daemon under
        # our captured pipe (the "Spawning…" hang). Order matters.
        new_sess_argv = _argv(calls, "new-session")
        self.assertLess(calls.index(_argv(calls, "start-server")), calls.index(new_sess_argv))
        self.assertEqual(
            new_sess_argv,
            ["tmux", "new-session", "-d", "-s", "fleet",
             "-P", "-F", "#{pane_id}", "-c", "/tmp",
             _EXE, "--dangerously-skip-permissions"],
        )

    def test_cold_start_leaves_the_config_error_view_mode(self):
        # A fresh server with a bad tmux config opens its first pane in view-mode
        # to show the error; every key sent there would be eaten by the mode, so
        # the spawn has to cancel it before the trust prompt is answered.
        calls = []
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch.object(tmux, "_venv_bin_dirs", return_value=set()):
            with _patch_run(_server(calls, sessions=(), pane="%1", in_mode=True)):
                r = tmux.new_window("/tmp")
        self.assertTrue(r["ok"])
        cancel = ["tmux", "send-keys", "-t", "%1", "-X", "cancel"]
        self.assertIn(cancel, calls)
        self.assertLess(calls.index(_argv(calls, "new-session")), calls.index(cancel))

    def test_spawned_command_force_unsets_board_venv_markers(self):
        # A long-lived tmux server started while the board's .venv was active
        # re-injects VIRTUAL_ENV into every new pane. When the board is in a venv,
        # the pane command must be wrapped in `env -u …` so the spawned session
        # can't inherit those markers regardless of the server's stale env.
        calls = []
        with mock.patch.dict("os.environ", {"PATH": "/opt/bin"}, clear=True), \
             mock.patch.object(tmux, "_venv_bin_dirs", return_value={"/board/.venv/bin"}):
            with _patch_run(_server(calls)):
                r = tmux.new_window("/tmp")
        self.assertTrue(r["ok"])
        new_win_argv = _argv(calls, "new-window")
        # `env -u VIRTUAL_ENV -u VIRTUAL_ENV_PROMPT -u PYTHONHOME` precedes `claude`.
        claude_idx = new_win_argv.index(_EXE)
        self.assertEqual(new_win_argv[claude_idx - 7:claude_idx],
                         ["env", "-u", "VIRTUAL_ENV",
                          "-u", "VIRTUAL_ENV_PROMPT", "-u", "PYTHONHOME"])

    def test_bare_path_pane_gets_the_cli_dir_appended(self):
        # A board started with a bare PATH finds claude in ~/.local/bin by
        # absolute path, but the pane inherits the bare PATH — so the session's
        # hooks could not find jq or claude there. The pane's PATH gains that
        # dir, appended so nothing the board's PATH already resolves is shadowed.
        calls = []
        with mock.patch.dict("os.environ", {"PATH": "/usr/bin:/bin"}, clear=True), \
             mock.patch.object(tmux, "_venv_bin_dirs", return_value=set()):
            with _patch_run(_server(calls)):
                r = tmux.new_window("/tmp")
        self.assertTrue(r["ok"])
        self.assertEqual(_argv(calls, "new-window")[-4:],
                         ["env", "PATH=/usr/bin:/bin:/opt/bin",
                          _EXE, "--dangerously-skip-permissions"])

    def test_cli_dir_follows_the_venv_unsets_on_the_board_path(self):
        # `env` takes `-u` options before assignments, and the base is the
        # spawn PATH — the board's own venv bin stays stripped.
        calls = []
        with mock.patch.dict("os.environ", {"PATH": "/board/.venv/bin:/usr/bin"},
                             clear=True), \
             mock.patch.object(tmux, "_venv_bin_dirs", return_value={"/board/.venv/bin"}):
            with _patch_run(_server(calls)):
                r = tmux.new_window("/tmp")
        self.assertTrue(r["ok"])
        new_win_argv = _argv(calls, "new-window")
        claude_idx = new_win_argv.index(_EXE)
        self.assertEqual(new_win_argv[claude_idx - 8:claude_idx],
                         ["env", "-u", "VIRTUAL_ENV", "-u", "VIRTUAL_ENV_PROMPT",
                          "-u", "PYTHONHOME", "PATH=/usr/bin:/opt/bin"])

    def test_empty_board_path_gets_just_the_cli_dir(self):
        # No leading separator: an empty PATH entry means the cwd.
        calls = []
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch.object(tmux, "_venv_bin_dirs", return_value=set()):
            with _patch_run(_server(calls)):
                tmux.new_window("/tmp")
        self.assertIn("PATH=/opt/bin", _argv(calls, "new-window"))

    def test_new_window_nonzero_exit_returns_error(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with _patch_run(_server(window_error="can't create window")):
                r = tmux.new_window("/tmp")
        self.assertFalse(r["ok"])
        self.assertIn("create window", r["error"])

    def test_env_target_missing_from_sessions_is_created_on_demand(self):
        # A pinned FLEET_TMUX_SESSION that doesn't exist yet is created (named),
        # not treated as an error — the env var names the host session to use.
        calls = []
        with mock.patch.dict("os.environ", {"FLEET_TMUX_SESSION": "ghost"}, clear=True):
            with _patch_run(_server(calls, sessions=["alpha", "beta"], pane="%7")):
                r = tmux.new_window("/tmp")
        self.assertTrue(r["ok"])
        self.assertIn("ghost", _argv(calls, "new-session"))
        self.assertFalse(any("new-window" in a for a in calls))

    def test_missing_cli_fails_before_opening_a_window(self):
        # The board's PATH is what the pane gets, not the tmux server's. When
        # the board cannot see the CLI, spawning would open a window that dies
        # 127 and disappears — a "success" with no card behind it. Refuse.
        calls = []
        with _pin_cli(None), mock.patch.dict("os.environ", {}, clear=True):
            with _patch_run(_server(calls)):
                r = tmux.new_window("/tmp")
        self.assertFalse(r["ok"])
        self.assertIn("claude not found", r["error"])
        self.assertFalse(any("new-window" in a for a in calls))


class SpawnLandedTests(unittest.TestCase):
    """The post-spawn probe: a pane id is not yet a running session."""

    def setUp(self):
        for patcher in (_pin_cli(),
                        mock.patch.object(tmux, "_SPAWN_LANDED_WAITS", (0.0,))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_pane_that_dies_immediately_is_not_reported_as_spawned(self):
        # tmux prints a pane id for a window it created even when the command in
        # it exits before anyone looks. Re-probing the pane is what turns that
        # into an error the dashboard can show instead of a phantom spawn.
        # The window is already gone; only the old panes are listed.
        with mock.patch.dict("os.environ", {}, clear=True):
            with _patch_run(_server(panes="%1\t/dev/pts/1\talpha\t/tmp\n")):
                r = tmux.new_window("/tmp")
        self.assertFalse(r["ok"])
        self.assertEqual(r["pane_id"], "%9")
        self.assertIn("exited immediately", r["error"])

    def test_live_pane_is_reported_as_spawned(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with _patch_run(_server(panes="%9\t/dev/pts/9\talpha\t/tmp\n")):
                r = tmux.new_window("/tmp")
        self.assertTrue(r["ok"])
        self.assertEqual(r["pane_id"], "%9")


class ResolveCliTests(unittest.TestCase):
    def test_prefers_what_the_spawn_path_resolves(self):
        with mock.patch.object(tmux.shutil, "which", return_value="/usr/bin/claude") as w:
            self.assertEqual(tmux._resolve_cli("claude"), "/usr/bin/claude")
        self.assertEqual(w.call_args[0][0], "claude")

    def test_falls_back_to_user_install_when_path_is_bare(self):
        # The bare PATH a supervisor restart leaves behind has no ~/.local/bin,
        # which is exactly where claude and codex install themselves.
        with mock.patch.dict("os.environ", {"HOME": "/home/u"}), \
             mock.patch.object(tmux.shutil, "which", return_value=None), \
             mock.patch.object(tmux.os, "access",
                               lambda path, mode: path == "/home/u/.local/bin/claude"):
            self.assertEqual(tmux._resolve_cli("claude"), "/home/u/.local/bin/claude")

    def test_falls_back_to_conda_base_when_path_is_bare(self):
        # hmz is a pip entry point in the conda base, which only .bashrc's
        # `conda init` block puts on PATH.
        with mock.patch.dict("os.environ", {"HOME": "/home/u"}), \
             mock.patch.object(tmux.shutil, "which", return_value=None), \
             mock.patch.object(tmux.os, "access",
                               lambda path, mode: path == "/home/u/miniconda3/bin/hmz"):
            self.assertEqual(tmux._resolve_cli("hmz"), "/home/u/miniconda3/bin/hmz")

    def test_returns_none_when_nothing_on_disk_matches(self):
        with mock.patch.object(tmux.shutil, "which", return_value=None), \
             mock.patch.object(tmux.os, "access", return_value=False):
            self.assertIsNone(tmux._resolve_cli("codex"))

    def test_absolute_path_is_taken_as_given(self):
        with mock.patch.object(tmux.os, "access", return_value=True):
            self.assertEqual(tmux._resolve_cli("/opt/claude"), "/opt/claude")


class SendTextTests(unittest.TestCase):
    def setUp(self):
        # Every send that gets as far as Enter then waits out the submit-verify;
        # record the waits instead of sleeping them.
        self.sleeps = []
        patcher = mock.patch.object(tmux.time, "sleep", side_effect=self.sleeps.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_sends_literal_then_separate_enter(self):
        calls = []
        with _patch_run(_recorder(calls)):
            r = tmux.send_text("%5", "hello world", marker="❯")
        self.assertTrue(r["ok"])
        self.assertEqual(
            calls[0],
            ["tmux", "send-keys", "-t", "%5", "-l", "--", "hello world"],
        )
        self.assertEqual(calls[1], ["tmux", "send-keys", "-t", "%5", "Enter"])

    def test_literal_failure_short_circuits_before_enter(self):
        calls = []
        with _patch_run(_recorder(calls, returncode=1, stderr="bad pane")):
            r = tmux.send_text("%5", "hi", marker="❯")
        self.assertFalse(r["ok"])
        self.assertEqual(len(calls), 1)  # Enter never sent

    def test_slash_prefix_settles_before_enter(self):
        calls = []
        with _patch_run(_recorder(calls)):
            r = tmux.send_text("%5", "/research-pipeline", marker="❯")
        self.assertTrue(r["ok"])
        self.assertEqual(self.sleeps[0], tmux._SLASH_SETTLE)
        self.assertEqual(calls[1], ["tmux", "send-keys", "-t", "%5", "Enter"])

    def test_settle_before_enter_pauses_plain_text(self):
        calls = []
        with _patch_run(_recorder(calls)):
            r = tmux.send_text("%5", "hello", settle_before_enter=tmux._CODEX_ENTER_SETTLE, marker="❯")
        self.assertTrue(r["ok"])
        # The settle before Enter, then the submit-verify's wait after it.
        self.assertEqual(self.sleeps, [tmux._CODEX_ENTER_SETTLE, tmux._SUBMIT_VERIFY_WAIT])
        self.assertEqual(calls[1], ["tmux", "send-keys", "-t", "%5", "Enter"])

    def test_slash_settle_wins_when_longer_than_caller_settle(self):
        # A slash prompt with a smaller caller settle still waits the slash time.
        with _patch_run(_recorder([])):
            r = tmux.send_text("%5", "/foo", settle_before_enter=0.1, marker="❯")
        self.assertTrue(r["ok"])
        self.assertEqual(self.sleeps[0], tmux._SLASH_SETTLE)

    def test_plain_text_does_not_settle_before_enter(self):
        with _patch_run(_recorder([])):
            r = tmux.send_text("%5", "research-pipeline", marker="❯")
        self.assertTrue(r["ok"])
        self.assertEqual(self.sleeps, [tmux._SUBMIT_VERIFY_WAIT])  # only the post-Enter check

    def test_enter_failure_is_reported(self):
        def fake_run(argv, **kw):
            if argv[-1] == "Enter":
                return FakeProc(returncode=1, stderr="enter failed")
            return FakeProc(returncode=0)

        with _patch_run(fake_run):
            r = tmux.send_text("%5", "hi", marker="❯")
        self.assertFalse(r["ok"])
        self.assertIn("enter failed", r["error"])


class ExitCopyModeTests(unittest.TestCase):
    """A pane in copy-mode (mouse scroll / view-mode) eats injected keystrokes;
    exit_copy_mode must kick it back to the TUI before any send."""

    def test_cancels_when_pane_in_copy_mode(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            if argv[-1] == "#{pane_in_mode}":
                return FakeProc(returncode=0, stdout="1\n")
            return FakeProc(returncode=0)

        with _patch_run(fake_run):
            tmux.exit_copy_mode("%5")
        self.assertIn(["tmux", "send-keys", "-t", "%5", "-X", "cancel"], calls)

    def test_noop_when_not_in_mode(self):
        calls = []
        with _patch_run(_recorder(calls, stdout="0\n")):
            tmux.exit_copy_mode("%5")
        self.assertEqual(len(calls), 1)  # probe only, no cancel

    def test_noop_when_probe_fails(self):
        calls = []
        with _patch_run(_recorder(calls, returncode=1, stderr="no such pane")):
            tmux.exit_copy_mode("%5")
        self.assertEqual(len(calls), 1)


class SendTextVerifySubmitTests(unittest.TestCase):
    """send_text confirms the composer emptied and resends Enter."""

    def test_no_resend_when_composer_already_empty(self):
        calls = []
        with _patch_run(_recorder(calls)), \
                mock.patch.object(tmux.time, "sleep"), \
                mock.patch.object(tmux, "_composer_has_tail", return_value=False):
            r = tmux.send_text("%5", "hello", marker="❯")
        self.assertTrue(r["ok"])
        enters = [c for c in calls if c[-1] == "Enter"]
        self.assertEqual(len(enters), 1)  # submit Enter only, no resend

    def test_resends_enter_until_composer_clears(self):
        calls = []
        states = iter([True, False])  # stranded once, then submitted
        with _patch_run(_recorder(calls)), \
                mock.patch.object(tmux.time, "sleep"), \
                mock.patch.object(tmux, "_composer_has_tail",
                                  side_effect=lambda *a: next(states)):
            r = tmux.send_text("%5", "hello", marker="❯")
        self.assertTrue(r["ok"])
        enters = [c for c in calls if c[-1] == "Enter"]
        self.assertEqual(len(enters), 2)  # initial submit + one resend

    def test_reports_failure_when_prompt_never_submits(self):
        calls = []
        with _patch_run(_recorder(calls)), \
                mock.patch.object(tmux.time, "sleep"), \
                mock.patch.object(tmux, "_composer_has_tail", return_value=True):
            r = tmux.send_text("%5", "hello", marker="❯")
        self.assertFalse(r["ok"])
        self.assertIn("unsent", r["error"])

    def test_slash_prompt_auto_verifies_submit(self):
        # Claude's slash popup can eat the submit Enter (it selects the
        # highlighted completion instead), silently stranding e.g. "/clear" in
        # the composer. Slash prompts must verify-and-resend.
        calls = []
        states = iter([True, False])  # stranded once, then submitted
        with _patch_run(_recorder(calls)), \
                mock.patch.object(tmux.time, "sleep"), \
                mock.patch.object(tmux, "_composer_has_tail",
                                  side_effect=lambda *a: next(states)):
            r = tmux.send_text("%5", "/clear", marker="❯")
        self.assertTrue(r["ok"])
        enters = [c for c in calls if c[-1] == "Enter"]
        self.assertEqual(len(enters), 2)  # initial submit + one resend

    def test_open_btw_overlay_does_not_trigger_enter_resend(self):
        # A submitted /btw keeps its command text on the composer line while the
        # answer overlay is open, and an Enter would DISMISS that overlay — the
        # aside dies mid-answer with nothing archived. The verify pass must read
        # the overlay as "submitted", never as a stranded prompt to re-Enter.
        overlay_screen = (
            "❯ /btw hello, just reply ok\n"
            "▔▔▔▔▔▔▔▔▔▔▔▔\n"
            "  /btw hello, just reply ok\n"
            "    ✽ Answering…\n"
            "  Esc to close\n"
        )
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            if "capture-pane" in argv:
                return FakeProc(returncode=0, stdout=overlay_screen)
            return FakeProc(returncode=0)

        with _patch_run(fake_run), \
                mock.patch.object(tmux.time, "sleep"):
            r = tmux.send_text("%5", "/btw hello, just reply ok", marker="❯")
        self.assertTrue(r["ok"])
        enters = [c for c in calls if c[-1] == "Enter"]
        self.assertEqual(len(enters), 1)  # submit Enter only — overlay untouched


# Claude's task list, which it draws BELOW the composer: every blocked task
# carries a `›`, Codex's composer glyph.
_CLAUDE_TASK_LIST = (
    "  10 tasks (0 done, 1 in progress, 9 open)\n"
    "  ◼ task1: New pool mining + eligibility + dedup\n"
    "  ◻ task2: Render spec v5 › blocked by #1\n"
    "  ◻ task3: Objective function › blocked by #2\n"
)


def _claude_pane_with_tasks(composer=""):
    """A Claude pane holding `composer` in its composer, task list below."""
    return f"────────────\n❯ {composer}\n────────────\n" + _CLAUDE_TASK_LIST


class SendTextVerifyLandedTests(unittest.TestCase):
    """verify_landed confirms the literal text reached the composer before Enter,
    re-sending it (clearing the composer first) when a busy-pane re-render dropped it."""

    def test_empty_composer_over_a_task_list_is_not_cleared_blind(self):
        # Read with Codex's glyph too, the empty composer "held" the last task
        # line's "blocked by #2": no clearing press could empty that, so every
        # landed-verify attempt ended in the blind fallback — forty clearing
        # presses into a composer that held nothing.
        typed = [""]
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            if "capture-pane" in argv:
                return FakeProc(stdout=_claude_pane_with_tasks(typed[0]))
            if "-l" in argv:
                typed[0] = argv[-1]
            elif argv[-1] == "Enter":
                typed[0] = ""
            return FakeProc()

        with _patch_run(fake_run), \
                mock.patch.object(tmux.time, "sleep"):
            r = tmux.send_text("%5", "hello", verify_landed=True, marker="❯")
        self.assertTrue(r["ok"])
        self.assertEqual(ClearComposerTests._presses(calls), [])

    def test_no_resend_when_text_lands_first_try(self):
        calls = []
        with _patch_run(_recorder(calls)), \
                mock.patch.object(tmux.time, "sleep"), \
                mock.patch.object(tmux, "_clear_composer") as cc, \
                mock.patch.object(tmux, "_tail_in",
                                  side_effect=[True, False]):  # landed, then submitted
            r = tmux.send_text("%5", "hello", verify_landed=True, marker="❯")
        self.assertTrue(r["ok"])
        literals = [c for c in calls if "-l" in c]
        self.assertEqual(len(literals), 1)  # text sent once
        # Landing first try still costs one clear: the composer is emptied
        # before the text is typed, never after it lands.
        cc.assert_called_once_with("%5", "❯")

    def test_composer_is_cleared_before_the_first_keystroke(self):
        # Regression: the clear used to run only on RETRIES, so whatever a
        # previous send left in the composer took the lead and our text was
        # appended to it. Because the landed check matches a tail, the
        # concatenation passed as a clean landing and Enter submitted it —
        # the board's Clear button on a wedged session stranded "/clear", and
        # the next press submitted the literal text "/clear/clear".
        order = []

        def fake_run(argv, **kw):
            if "-l" in argv:
                order.append("type")
            return FakeProc(returncode=0)

        with _patch_run(fake_run), \
                mock.patch.object(tmux.time, "sleep"), \
                mock.patch.object(tmux, "_clear_composer",
                                  side_effect=lambda *a: order.append("clear")), \
                mock.patch.object(tmux, "_tail_in",
                                  side_effect=[True, False]):  # landed, then submitted
            r = tmux.send_text("%5", "/clear", verify_landed=True, marker="❯")
        self.assertTrue(r["ok"])
        # The clear precedes the very first keystroke, not just the resends.
        self.assertEqual(order[:2], ["clear", "type"])

    def test_resends_text_after_clearing_when_dropped_once(self):
        calls = []
        landed = iter([False, True, False])  # dropped once, lands, submits
        with _patch_run(_recorder(calls)), \
                mock.patch.object(tmux.time, "sleep"), \
                mock.patch.object(tmux, "_clear_composer") as cc, \
                mock.patch.object(tmux, "_tail_in",
                                  side_effect=lambda *a: next(landed)):
            r = tmux.send_text("%5", "hello", verify_landed=True, marker="❯")
        self.assertTrue(r["ok"])
        literals = [c for c in calls if "-l" in c]
        self.assertEqual(len(literals), 2)  # initial + one resend
        # Every attempt clears first, so neither a leftover from an earlier send
        # nor a partial paste can concatenate into a corrupted prompt.
        self.assertEqual(cc.call_args_list, [mock.call("%5", "❯")] * 2)

    def test_reports_failure_when_text_never_lands(self):
        calls = []
        with _patch_run(_recorder(calls)), \
                mock.patch.object(tmux.time, "sleep"), \
                mock.patch.object(tmux, "_clear_composer") as cc, \
                mock.patch.object(tmux, "_tail_in", return_value=False):
            r = tmux.send_text("%5", "hello", verify_landed=True, marker="❯")
        self.assertFalse(r["ok"])
        self.assertIn("never landed", r["error"])
        # Never press Enter on a prompt that never made it into the composer.
        self.assertNotIn(["tmux", "send-keys", "-t", "%5", "Enter"], calls)
        # A stalled TUI buffers the keystrokes rather than dropping them — they
        # land AFTER we give up and would strand in the composer, corrupting the
        # next send. Giving up must end with a cleanup clear so the buffered
        # text is wiped whenever the pane wakes: one clear per attempt plus the
        # final cleanup.
        self.assertEqual(cc.call_count, len(tmux._LANDED_VERIFY_WAITS) + 1)

    def test_landed_waits_escalate_for_laggy_panes(self):
        # A busy pane can take well over 0.15s to echo the paste; the between-
        # check waits must escalate so ordinary render lag doesn't get
        # misreported as a dropped prompt.
        sleeps = []
        with _patch_run(_recorder([])), \
                mock.patch.object(tmux.time, "sleep", side_effect=sleeps.append), \
                mock.patch.object(tmux, "_tail_in", return_value=False):
            r = tmux.send_text("%5", "hello", verify_landed=True, marker="❯")
        self.assertFalse(r["ok"])
        self.assertEqual(tuple(sleeps), tmux._LANDED_VERIFY_WAITS)
        self.assertEqual(sleeps, sorted(sleeps))  # never shrinks
        self.assertGreaterEqual(sum(sleeps), 3.0)  # rides out multi-second lag

    def test_landed_then_verifies_submit_together(self):
        # The two phases compose: text lands before Enter, composer empties after.
        calls = []
        with _patch_run(_recorder(calls)), \
                mock.patch.object(tmux.time, "sleep"), \
                mock.patch.object(tmux, "_tail_in",
                                  side_effect=[True, False]):  # landed, then submitted
            r = tmux.send_text("%5", "hello", verify_landed=True, marker="❯")
        self.assertTrue(r["ok"])
        enters = [c for c in calls if c[-1] == "Enter"]
        self.assertEqual(len(enters), 1)  # submitted on first Enter, no resend


class SendTextConfirmedTests(unittest.TestCase):
    """send_text_confirmed (hmz) pastes once, waits out a slow intake, and
    succeeds only on the caller's evidence that the line was taken."""

    def setUp(self):
        self.now = [1000.0]
        self.calls = []
        self.inputs = []

    def _run(self, *extra):
        def fake_run(argv, **kw):
            self.calls.append(argv)
            self.inputs.append(kw.get("input"))
            return FakeProc(returncode=0)
        sleep = lambda s: self.now.__setitem__(0, self.now[0] + s)
        return [mock.patch.object(tmux.subprocess, "run", side_effect=fake_run),
                mock.patch.object(tmux.time, "sleep", side_effect=sleep),
                mock.patch.object(tmux.time, "time", side_effect=lambda: self.now[0]),
                mock.patch.object(tmux, "_clear_composer"), *extra]

    def _send(self, landed, took, text="hello", refused=None):
        patches = self._run(mock.patch.object(tmux, "_composer_has_tail", side_effect=landed))
        for p in patches:
            p.start()
        try:
            return tmux.send_text_confirmed("%5", text, took, "❯", refused=refused)
        finally:
            for p in reversed(patches):
                p.stop()

    def _literals(self):
        return [c for c in self.calls if "paste-buffer" in c or "-l" in c]

    def _enters(self):
        return [c for c in self.calls if c[-1] == "Enter"]

    def test_text_goes_in_as_one_bracketed_paste(self):
        # Typed a key at a time, 1700 chars took hmz 2.4s to ingest; pasted, 0.06s.
        r = self._send(lambda *a: True, took=lambda: True, text="split the todo")
        self.assertTrue(r["ok"])
        load = next(c for c in self.calls if "load-buffer" in c)
        paste = next(c for c in self.calls if "paste-buffer" in c)
        self.assertEqual(self.inputs[self.calls.index(load)], "split the todo")
        name = load[load.index("-b") + 1]
        self.assertEqual(paste[paste.index("-b") + 1], name)
        for flag in ("-p", "-r", "-d"):
            self.assertIn(flag, paste)
        self.assertEqual(paste[paste.index("-t") + 1], "%5")
        self.assertFalse(any("-l" in c for c in self.calls))  # no per-key typing

    def test_a_failed_paste_leaves_no_buffer_and_no_enter(self):
        def fake_run(argv, **kw):
            self.calls.append(argv)
            return FakeProc(returncode=1 if "paste-buffer" in argv else 0, stderr="no pane")
        with _patch_run(fake_run), \
                mock.patch.object(tmux, "_clear_composer"):
            r = tmux.send_text_confirmed("%5", "hello", lambda: True, "❯")
        self.assertFalse(r["ok"])
        self.assertTrue(any("delete-buffer" in c for c in self.calls))
        self.assertEqual(self._enters(), [])

    def test_slow_intake_is_waited_out_not_retyped(self):
        # Regression: a 0.15s check missed hmz still ingesting, and the clear +
        # retype raced the first copy into an empty editor.
        landed = iter([False] * 5 + [True])
        r = self._send(lambda *a: next(landed), took=lambda: True)
        self.assertTrue(r["ok"])
        self.assertEqual(len(self._literals()), 1)
        self.assertEqual(len(self._enters()), 1)

    def test_emptied_composer_without_evidence_is_a_failure(self):
        # The live loss: Enter, the composer reads empty, hmz never took it.
        tail = iter([True, False, False])
        r = self._send(lambda *a: next(tail), took=lambda: False)
        self.assertFalse(r["ok"])
        self.assertIn("never taken", r["error"])
        self.assertEqual(len(self._enters()), 1)  # an Enter more wouldn't help

    def test_stranded_text_gets_enter_again(self):
        polls = round(tmux._CONFIRMED_TOOK_WAIT / tmux._CONFIRMED_POLL)  # one Enter's full wait
        took = iter([False] * polls + [True])
        r = self._send(lambda *a: True, took=lambda: next(took))
        self.assertTrue(r["ok"])
        self.assertEqual(len(self._enters()), 2)

    def test_never_landed_presses_no_enter(self):
        r = self._send(lambda *a: False, took=lambda: True)
        self.assertFalse(r["ok"])
        self.assertIn("never landed", r["error"])
        self.assertEqual(self._enters(), [])
        self.assertEqual(len(self._literals()), 1)

    def test_taken_then_refused_is_not_sent(self):
        # The live miss: hmz wrote `$parallel_flame_chase …` to its history, then
        # had no such flow, and the board said sent.
        said = iter(["", "", "hmz: no such flow: parallel_flame_chase"])
        r = self._send(lambda *a: True, took=lambda: True, refused=lambda: next(said))
        self.assertFalse(r["ok"])
        self.assertIn("hmz: no such flow: parallel_flame_chase", r["error"])
        self.assertEqual(len(self._enters()), 1)

    def test_taken_and_not_refused_waits_out_the_window(self):
        r = self._send(lambda *a: True, took=lambda: True, refused=lambda: "")
        self.assertTrue(r["ok"])
        self.assertGreaterEqual(self.now[0] - 1000.0, tmux._CONFIRMED_REFUSAL_WAIT)

    def test_landing_wait_grows_with_length_and_is_capped(self):
        for n, wait in ((10, 3.03), (1000, 6.0), (100000, tmux._CONFIRMED_LANDED_MAX)):
            self.now, self.calls = [1000.0], []
            self._send(lambda *a: False, took=lambda: True, text="x" * n)
            self.assertAlmostEqual(self.now[0] - 1000.0, wait, delta=tmux._CONFIRMED_POLL)


class ShownAboveComposerTests(unittest.TestCase):
    PANE = ("\n".join([
        "split the todo across workers",
        "── assistant",
        "● assistant is working",
        "─" * 40,
        "❯ ",
        "─" * 40,
        "  ◉ chat · /home/u/proj",
    ]))

    def _shown(self, screen, text="split the todo across workers"):
        with mock.patch.object(tmux, "capture_pane", return_value={"ok": True, "text": screen}):
            return tmux._shown_above_composer("%1", text, "❯")

    def test_echo_above_an_empty_composer(self):
        self.assertTrue(self._shown(self.PANE))

    def test_text_still_in_the_composer_is_not_an_echo(self):
        self.assertFalse(self._shown(self.PANE.replace("❯ ", "❯ split the todo across workers")))

    def test_absent(self):
        self.assertFalse(self._shown(self.PANE, text="something else entirely"))


class ComposerHasTailTests(unittest.TestCase):
    """_composer_has_tail anchors on the DRIVEN platform's composer marker only —
    Claude's `❯` by default, Codex's `›` when passed. The other TUI's glyph is
    ordinary content on the pane (Claude renders `›` as a task-list separator
    BELOW the composer), so searching for both would steal the anchor."""

    def _tail(self, cap_text, text, marker="❯"):
        with mock.patch.object(tmux, "capture_pane",
                               return_value={"ok": True, "text": cap_text}):
            return tmux._composer_has_tail("%5", text, marker)

    def test_claude_text_landed_with_tasklist_below_composer(self):
        # Claude renders the todo summary BELOW the input composer, and each
        # blocked task carries a `›` separator. Anchoring on the LAST `›` (a task
        # line, below our text) would look past the composer and wrongly report
        # the prompt never landed. The ❯ composer holds our text → landed.
        cap = (
            "  ✻ Cogitated for 2m 10s\n"
            "────────────\n"
            "❯ start the training run now\n"
            "────────────\n"
            "  10 tasks (0 done, 1 in progress, 9 open)\n"
            "  ◼ task1: New pool mining + eligibility + dedup\n"
            "  ◻ task2: Render spec v5 › blocked by #1\n"
            "  ◻ task3: Objective function › blocked by #2\n"
        )
        self.assertTrue(self._tail(cap, "start the training run now"))

    def test_codex_stranded_prompt_is_detected_with_codex_marker(self):
        cap = (
            "────────────\n"
            "› run the benchmark suite\n"
            "────────────\n"
        )
        self.assertTrue(self._tail(cap, "run the benchmark suite", marker="›"))

    def test_claude_stranded_slash_command_is_detected(self):
        cap = (
            "✻ Cogitated for 3m 17s\n"
            "\n"
            "────────────\n"
            "❯ /clear\n"
            "────────────\n"
            "  ⏵⏵ bypass permissions on\n"
        )
        self.assertTrue(self._tail(cap, "/clear"))

    def test_claude_echoed_turn_above_empty_composer_is_not_stranded(self):
        # A submitted prompt is echoed as a turn ABOVE the composer, with the
        # same ❯ marker; only the LAST marker is the composer, and it is empty.
        cap = (
            "❯ /clear\n"
            "  ⎿ cleared\n"
            "────────────\n"
            "❯ \n"
            "────────────\n"
            "  ⏵⏵ bypass permissions on\n"
        )
        self.assertFalse(self._tail(cap, "/clear"))

    def test_shell_echo_without_composer_marker_is_not_landed(self):
        # If the TUI died or was suspended, its parent shell owns the pty: the
        # injected text is echoed at a bash prompt with NO composer marker on
        # screen. Treating that echo as "landed" would make send_text press
        # Enter and EXECUTE the prompt as a shell command. No marker → False.
        cap = (
            "(base) user@host:~/repo$ \n"
            "(base) user@host:~/repo$ this prompt must NOT submit\n"
        )
        self.assertFalse(self._tail(cap, "this prompt must NOT submit"))

    def test_btw_overlay_with_command_still_on_composer_is_not_stranded(self):
        # While a /btw aside is open the composer line still shows the command
        # AND the overlay echoes it below — but the "Esc to close" footer proves
        # the submit landed, so the text must not be treated as stranded.
        cap = (
            "────────────\n"
            "❯ /btw hello, just reply ok\n"
            "▔▔▔▔▔▔▔▔▔▔▔▔\n"
            "  /btw hello, just reply ok\n"
            "    Ok.\n"
            "  ↑/↓ to scroll · c to copy · f to fork · Esc to close\n"
        )
        self.assertFalse(self._tail(cap, "/btw hello, just reply ok"))

    def test_collapsed_paste_placeholder_counts_as_our_text(self):
        # Claude collapses a paste past ~1000 chars into a "[Pasted text #N]"
        # placeholder — the literal tail is never on screen, so requiring it
        # made EVERY long prompt fail landed-verify ("prompt text never landed
        # in composer", seen live on v2.1.211). The placeholder in the composer
        # region IS our text: Claude holds the full content and expands it on
        # submit.
        cap = (
            "────────────\n"
            "❯ [Pasted text #13]\n"
            "────────────\n"
            "  paste again to expand\n"
        )
        self.assertTrue(self._tail(cap, "很长的提示内容" * 200))

    def test_placeholder_wrapped_after_leading_text_still_counts(self):
        # A collapsed paste appended after text already in the composer can
        # soft-wrap mid-placeholder; the check must survive the line break.
        cap = (
            "────────────\n"
            "❯ 前置文字前置文字前置文字前置文字前置文字前置文字 [Pasted text\n"
            "  #4]\n"
            "────────────\n"
        )
        self.assertTrue(self._tail(cap, "很长的提示内容" * 200))

    def test_placeholder_in_echoed_turn_above_composer_is_not_stranded(self):
        # A submitted long prompt is echoed as a turn above the composer with
        # the same placeholder rendering; only the region after the LAST marker
        # (the empty composer) counts.
        cap = (
            "❯ [Pasted text #2]\n"
            "● done\n"
            "────────────\n"
            "❯ \n"
            "────────────\n"
        )
        self.assertFalse(self._tail(cap, "很长的提示内容" * 200))


class ComposerTextTests(unittest.TestCase):
    """_composer_text reads what is sitting in the composer: the last marker
    line plus wrapped continuation lines, stopping at the chrome below."""

    def test_empty_composer_reads_empty(self):
        cap = (
            "✻ Churned for 8s\n"
            "────────────\n"
            "❯ \n"
            "────────────\n"
            "  ⏵⏵ bypass permissions on\n"
        )
        self.assertEqual(tmux._composer_text(cap, "❯"), "")

    def test_wrapped_content_is_joined_across_lines(self):
        cap = (
            "────────────\n"
            "❯ ABC0123456789012345678901234567890123456789\n"
            "  678901234567890123456789012345678901234567\n"
            "────────────\n"
            "  ⏵⏵ bypass permissions on\n"
        )
        text = tmux._composer_text(cap, "❯")
        self.assertIn("ABC", text)
        self.assertIn("6789012345678901234567890123456789", text)

    def test_status_line_without_rule_is_not_content(self):
        # Minimal layouts draw the status line directly under the marker line.
        cap = "❯ \n⏵⏵ bypass permissions on\n"
        self.assertEqual(tmux._composer_text(cap, "❯"), "")

    def test_no_marker_returns_none(self):
        self.assertIsNone(tmux._composer_text("(base) user@host:~$ \n", "❯"))

    def test_task_list_below_an_empty_claude_composer_is_not_its_text(self):
        self.assertEqual(tmux._composer_text(_claude_pane_with_tasks(), "❯"), "")

    def test_codex_composer_is_read_on_its_own_marker(self):
        cap = (
            "────────────\n"
            "› run the benchmark suite\n"
            "────────────\n"
        )
        self.assertEqual(tmux._composer_text(cap, "›"), "run the benchmark suite")


class ClearComposerTests(unittest.TestCase):
    """_clear_composer must send one clearing press (End, Ctrl-U, Backspace) per
    visual LINE (Claude's composer removes only one line per press), verified
    against the pane, falling back to blind presses when the pane won't redraw."""

    @staticmethod
    def _presses(calls):
        n = len(tmux._CLEAR_KEYS)
        return [c for c in calls if tuple(c[-n:]) == tmux._CLEAR_KEYS]

    def test_press_is_end_then_ctrl_u_then_backspace(self):
        # Ctrl-U alone only kills LEFT of the cursor on its visual row (live on
        # v2.1.286/287): a leftover with the cursor mid-line kept its tail and the
        # next prompt went out with it appended. End first takes the whole row;
        # Backspace joins an emptied row onto the one above. Pin the order: the
        # keys go in one send-keys.
        calls = []
        screens = iter(["❯ aaa\n  bbb\n────────────\n", "❯ \n────────────\n"])
        with _patch_run(_recorder(calls)), \
                mock.patch.object(tmux.time, "sleep"), \
                mock.patch.object(tmux, "capture_pane",
                                  side_effect=lambda *a, **k: {"ok": True, "text": next(screens)}):
            tmux._clear_composer("%5", "❯")
        self.assertEqual(tmux._CLEAR_KEYS, ("End", "C-u", "BSpace"))
        sends = [c for c in calls if "send-keys" in c]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][-3:], ["End", "C-u", "BSpace"])

    def test_presses_once_per_line_until_empty(self):
        screens = iter([
            "❯ line-a line-a line-a\n  line-b line-b\n────────────\n",
            "❯ line-a line-a line-a\n────────────\n",
            "❯ \n────────────\n",
        ])
        calls = []
        with _patch_run(_recorder(calls)), \
                mock.patch.object(tmux.time, "sleep"), \
                mock.patch.object(tmux, "capture_pane",
                                  side_effect=lambda *a, **k: {"ok": True, "text": next(screens)}):
            tmux._clear_composer("%5", "❯")
        self.assertEqual(len(self._presses(calls)), 2)  # one per non-empty read, none once empty

    def test_already_empty_composer_sends_nothing(self):
        for name, screen in (("bare", "❯ \n────────────\n"),
                             ("task list below", _claude_pane_with_tasks())):
            with self.subTest(name):
                calls = []
                with _patch_run(_recorder(calls)), \
                        mock.patch.object(tmux.time, "sleep"), \
                        mock.patch.object(tmux, "capture_pane",
                                          return_value={"ok": True, "text": screen}):
                    tmux._clear_composer("%5", "❯")
                self.assertEqual(self._presses(calls), [])

    def test_stalled_pane_falls_back_to_blind_presses(self):
        # A stalled TUI never redraws: the same screen comes back after a press
        # (no progress), so the loop must stop reading and queue enough blind
        # presses to clear a worst-case wrapped paste whenever the pane wakes.
        calls = []
        with _patch_run(_recorder(calls)), \
                mock.patch.object(tmux.time, "sleep"), \
                mock.patch.object(tmux, "capture_pane",
                                  return_value={"ok": True, "text": "❯ stuck text\n────────────\n"}):
            tmux._clear_composer("%5", "❯")
        self.assertGreaterEqual(len(self._presses(calls)), tmux._CLEAR_BLIND_PRESSES)

    def test_capture_failure_falls_back_to_blind_presses(self):
        calls = []
        with _patch_run(_recorder(calls)), \
                mock.patch.object(tmux.time, "sleep"), \
                mock.patch.object(tmux, "capture_pane", return_value={"ok": False}):
            tmux._clear_composer("%5", "❯")
        self.assertEqual(len(self._presses(calls)), tmux._CLEAR_BLIND_PRESSES)


class CodexEnterSettleTests(unittest.TestCase):
    def test_scales_with_length_and_caps(self):
        self.assertEqual(tmux.codex_enter_settle(0), tmux._CODEX_ENTER_SETTLE)
        # monotonic in length
        self.assertGreater(tmux.codex_enter_settle(4000), tmux.codex_enter_settle(500))
        # never exceeds the cap, even past the max prompt size
        self.assertEqual(tmux.codex_enter_settle(1_000_000), tmux._CODEX_ENTER_SETTLE_MAX)


class SpawnEnvTests(unittest.TestCase):
    """_spawn_env must hand spawned sessions a clean interpreter, not the board's."""

    def _env(self, overrides, *, prefix, base_prefix):
        with mock.patch.dict(tmux.os.environ, overrides, clear=True), \
             mock.patch.object(tmux.sys, "prefix", prefix), \
             mock.patch.object(tmux.sys, "base_prefix", base_prefix):
            return tmux._spawn_env()

    def test_strips_board_virtualenv_from_path(self):
        venv = "/board/.venv"
        env = self._env(
            {
                "VIRTUAL_ENV": venv,
                "PATH": f"{venv}/bin:/usr/bin:/bin",
                "PYTHONHOME": f"{venv}",
            },
            prefix=venv, base_prefix="/usr",
        )
        self.assertNotIn("VIRTUAL_ENV", env)
        self.assertNotIn("PYTHONHOME", env)
        self.assertNotIn(f"{venv}/bin", env["PATH"].split(":"))
        self.assertEqual(env["PATH"], "/usr/bin:/bin")

    def test_leaves_path_untouched_when_not_in_a_venv(self):
        env = self._env(
            {"PATH": "/usr/bin:/bin"},
            prefix="/usr", base_prefix="/usr",
        )
        self.assertEqual(env["PATH"], "/usr/bin:/bin")

    def test_still_strips_claude_child_session_markers(self):
        env = self._env(
            {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "abc", "PATH": "/usr/bin"},
            prefix="/usr", base_prefix="/usr",
        )
        self.assertNotIn("CLAUDECODE", env)
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", env)


class TrailingSemicolonEscapeTests(unittest.TestCase):
    """tmux's command parser splits a command sequence on an argument ending in
    an unescaped ";", even when that argument is a single argv element — so a
    prompt ending in ";" was typed minus its final character, the landed-verify
    needle (which keeps the ";") could never match, and the send failed as
    "stayed empty" after the give-up clear wiped the composer."""

    def test_literal_key_arg_escapes_only_a_trailing_semicolon(self):
        self.assertEqual(tmux._literal_key_arg("abc"), "abc")
        self.assertEqual(tmux._literal_key_arg("a;b"), "a;b")  # mid-text is safe
        self.assertEqual(tmux._literal_key_arg("abc;"), "abc\\;")
        self.assertEqual(tmux._literal_key_arg("a;;"), "a;\\;")  # last one only

    def _literal_calls(self, calls):
        return [c for c in calls if "-l" in c]

    def test_send_text_escapes_trailing_semicolon(self):
        calls = []
        with _patch_run(_recorder(calls)), \
                mock.patch.object(tmux.time, "sleep"):
            r = tmux.send_text("%5", "do the thing;", marker="❯")
        self.assertTrue(r["ok"])
        (literal,) = self._literal_calls(calls)
        self.assertEqual(literal[-1], "do the thing\\;")

    def test_landed_verify_sends_escaped_but_matches_raw_text(self):
        # The escape is a transport detail: the composer shows the raw ";", so
        # the landed-verify needle must keep matching against the RAW text.
        calls = []
        seen_tails = []
        def fake_tail(cap, text, marker="❯"):
            seen_tails.append(text)
            return len(seen_tails) == 1  # landed, then submitted
        with _patch_run(_recorder(calls)), \
                mock.patch.object(tmux.time, "sleep"), \
                mock.patch.object(tmux, "_tail_in", side_effect=fake_tail):
            r = tmux.send_text("%5", "goal 2. 重跑(钉死);", verify_landed=True, marker="❯")
        self.assertTrue(r["ok"])
        (literal,) = self._literal_calls(calls)
        self.assertEqual(literal[-1], "goal 2. 重跑(钉死)\\;")
        self.assertEqual(set(seen_tails), {"goal 2. 重跑(钉死);"})

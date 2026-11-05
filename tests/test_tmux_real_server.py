"""core/tmux.py's spawns on a real, private tmux server: where the window lands.

tests/test_tmux.py fakes every tmux call, so it can only check the command text.
These read back the session a spawned window is in. Each test runs its own
server on its own socket and kills it afterwards; without tmux they skip.
"""
import os
import shutil
import threading
import unittest
from unittest import mock

from core import tmux


@unittest.skipUnless(shutil.which("tmux"), "needs tmux")
class RealServerSpawnTests(unittest.TestCase):
    """new_window on a real, private tmux server: which session the window is in."""

    def setUp(self):
        name = f"cb-spawn-{os.getpid()}-{self._testMethodName}"
        env = mock.patch.dict("os.environ", {"FLEET_TMUX_SOCKET": name})
        env.start()
        self.addCleanup(env.stop)
        for k in ("FLEET_TMUX_SESSION", "TMUX"):
            os.environ.pop(k, None)
        self.addCleanup(lambda: os.path.exists(tmux._socket_path(name))
                        and os.unlink(tmux._socket_path(name)))
        self.addCleanup(tmux._run, "kill-server")
        tmux._clear_caches()

    def _session(self, name, window="main"):
        self.assertTrue(tmux._run("new-session", "-d", "-s", name, "-n", window, "sleep", "600")["ok"])

    def _landed(self, r):
        self.assertTrue(r["ok"], r)
        return tmux._display(r["pane_id"], "#{session_name}")

    def test_cards_go_to_fleet_beside_other_sessions(self):
        self._session("zzz", window="fleetish")
        self._session("7")
        self.assertEqual(self._landed(tmux.new_window("/tmp", ["sleep", "600"])), "fleet")
        self.assertEqual(self._landed(tmux.new_window("/tmp", ["sleep", "600"])), "fleet")

    def test_fleet_closing_mid_spawn_never_lands_in_a_lookalike(self):
        # list-sessions saw `fleet`; its last card closed before new-window ran.
        self._session("fleet-ops")
        with mock.patch.object(tmux, "_resolve_target",
                               return_value={"target": "fleet", "exists": True}):
            r = tmux.new_window("/tmp", ["sleep", "600"])
        self.assertFalse(r["ok"])
        windows = tmux._run("list-windows", "-t", "=fleet-ops:", "-F", "#{window_index}")
        self.assertEqual(windows["stdout"].split(), ["0"])

    def test_concurrent_first_spawns_both_land(self):
        self._session("other")
        real, barrier, results = tmux._resolve_target, threading.Barrier(2), []

        def both_see_fleet_missing():
            t = real()
            barrier.wait()
            return t

        with mock.patch.object(tmux, "_resolve_target", side_effect=both_see_fleet_missing):
            threads = [threading.Thread(target=lambda: results.append(
                tmux.new_window("/tmp", ["sleep", "600"]))) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual([self._landed(r) for r in results], ["fleet", "fleet"])

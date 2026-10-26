"""Tests for the GPU rows: who is on each card, gpu_wait claims and waiters,
and the join onto board cards.

nvidia-smi is stubbed at `core.gpus._smi`; everything else is real — the
processes are real children of the test, the claim locks are real flocks.
"""
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

import app as appmod
from core import gpus, peers
from tests.helpers import scratch_dir

UUID0, UUID1 = "GPU-aaaa", "GPU-bbbb"


def _smi_stub(apps):
    def smi(kind, fields):
        if kind == "gpu":
            return [["0", UUID0, "1000", "46068", "97"], ["1", UUID1, "0", "[N/A]", "[N/A]"]]
        return apps
    return smi


def _spawn(argv, **env):
    p = subprocess.Popen(argv, env=dict(os.environ, **env))
    time.sleep(0.2)  # let it exec, so /proc shows its own cmdline and environ
    return p


class SampleTests(unittest.TestCase):

    def setUp(self):
        self.procs = []
        self.addCleanup(lambda: [p.kill() for p in self.procs])

    def test_a_gpu_process_names_the_session_and_ssh_that_started_it(self):
        job = _spawn(["sleep", "30"], CLAUDE_CODE_SESSION_ID="sid-1",
                     SSH_CONNECTION="10.0.0.5 50000 10.0.0.6 22")
        self.procs.append(job)
        apps = [[UUID0, str(job.pid), "900"], [UUID0, "[N/A]", "100"]]
        with mock.patch.object(gpus, "_smi", side_effect=_smi_stub(apps)):
            s = gpus.sample()
        card0, card1 = s["cards"]
        self.assertEqual((card0["used_mb"], card0["total_mb"], card0["util"]), (1000, 46068, 97))
        self.assertEqual((card1["total_mb"], card1["util"]), (None, None), "a card CUDA can't see")
        mine, hidden = card0["procs"]
        self.assertEqual((mine["session"], mine["ssh_from"], mine["used_mb"]), ("sid-1", "10.0.0.5", 900))
        self.assertIn("sleep 30", mine["cmd"])
        self.assertLess(abs(mine["started_at"] - time.time()), 5)
        self.assertIsNone(hidden["pid"], "another container's process has no pid here")

    def test_no_nvidia_smi_means_no_sample_and_no_row(self):
        with mock.patch.object(gpus, "_smi", side_effect=FileNotFoundError("nvidia-smi")):
            self.assertIsNone(gpus.sample())
        with mock.patch.object(gpus, "_latest", None):
            self.assertEqual(gpus.local("a"), [])

    def test_claims_and_waiters_follow_the_gpu_wait_locks(self):
        """A held lock is a claim, one per holding pid with every ordinal it
        holds; a lock nobody holds is a finished claim; a gpu_wait.py with no
        claim is still waiting. The holder is whoever holds the flock: a
        process that flocks a file someone else wrote is named by its own
        command, not by the stale note inside."""
        tool = scratch_dir() / "gpu_wait.py"
        tool.write_text("import fcntl, os, sys, time\n"
                        "fds = [os.open(a, os.O_RDWR) for a in sys.argv[1:] if a.endswith('.lock')]\n"
                        "[fcntl.flock(fd, fcntl.LOCK_EX) for fd in fds]\n"
                        "time.sleep(30)\n")
        locks = scratch_dir()
        for name in ["gpu2.lock", "gpu3.s1.lock", "gpu5.lock", "gpu6.lock"]:
            (locks / name).write_text("pid=1 python finished.py\n")
        claimed = _spawn([sys.executable, str(tool), str(locks / "gpu2.lock"), str(locks / "gpu3.s1.lock"),
                          "--", "python", "job.py"])
        for name in ["gpu2.lock", "gpu3.s1.lock"]:
            (locks / name).write_text(f"pid={claimed.pid} slots=3 python job.py\n")
        foreign = _spawn([sys.executable, "-c", "import fcntl, os, sys, time; fd = os.open(sys.argv[1], os.O_RDWR); "
                          "fcntl.flock(fd, fcntl.LOCK_EX); time.sleep(30)", str(locks / "gpu5.lock")])
        waiter = _spawn([sys.executable, str(tool), "--need-gb", "40"])
        self.procs += [claimed, foreign, waiter]
        with mock.patch.object(gpus, "_smi", side_effect=_smi_stub([])), \
             mock.patch.dict(os.environ, {"FLEET_GPU_LOCK_DIR": str(locks)}):
            s = gpus.sample()
        claims = {c["pid"]: c for c in s["claims"]}
        self.assertEqual(sorted(claims), sorted([claimed.pid, foreign.pid]))
        self.assertEqual((claims[claimed.pid]["cuda"], claims[claimed.pid]["cmd"]), ([2, 3], "python job.py"))
        self.assertEqual(claims[foreign.pid]["cuda"], [5])
        self.assertNotIn("finished.py", claims[foreign.pid]["cmd"])
        # the machine running the tests may have gpu_wait.py processes of its own
        ours = [w["pid"] for w in s["waiting"] if w["pid"] in (claimed.pid, waiter.pid)]
        self.assertEqual(ours, [waiter.pid])

    def test_without_a_lock_dir_there_are_no_claims_or_waiters(self):
        with mock.patch.object(gpus, "_smi", side_effect=_smi_stub([])), \
             mock.patch.dict(os.environ, {"FLEET_GPU_LOCK_DIR": ""}):
            s = gpus.sample()
        self.assertEqual((s["claims"], s["waiting"]), ([], []))


class LocalRowTests(unittest.TestCase):

    def test_an_old_sample_is_stale_and_the_row_is_a_copy(self):
        sample = {"ts": time.time() - gpus.STALE_AFTER - 1, "claims": [], "waiting": [],
                  "cards": [{"index": 0, "procs": [{"pid": 1, "session": None}]}]}
        with mock.patch.object(gpus, "_latest", sample):
            (row,) = gpus.local("a")
            self.assertEqual((row["host"], row["stale"]), ("a", True))
            self.assertNotIn("ts", row, "ts moves every sample; stale says what it means")
            row["cards"][0]["procs"][0]["card"] = "1"
            self.assertNotIn("card", sample["cards"][0]["procs"][0])


def _row(host, index, *procs, claims=(), waiting=()):
    return {"host": host, "stale": False, "claims": list(claims), "waiting": list(waiting),
            "cards": [{"index": index, "procs": list(procs)}]}


class AttachTests(unittest.TestCase):

    def test_processes_name_their_card_and_cards_list_their_gpus(self):
        snap = {
            "windows": [{"key": "1", "session_id": "s1"}, {"key": "b:7", "session_id": "s2"},
                        {"key": "2"}],
            "gpus": [_row("a", 0, {"session": "s1"}, {"session": None},
                          claims=[{"session": "s2"}], waiting=[{"session": "s9"}]),
                     _row("b", 3, {"session": "s1"}, {"session": "s1"})],
        }
        gpus.attach(snap)
        a, b = snap["gpus"]
        self.assertEqual([p["card"] for p in a["cards"][0]["procs"]], ["1", None])
        self.assertEqual((a["claims"][0]["card"], a["waiting"][0]["card"]), ("b:7", None))
        self.assertEqual([w["gpus"] for w in snap["windows"]], [["a:0", "b:3"], [], []])


class PeerGpuTests(unittest.TestCase):

    def setUp(self):
        patch = mock.patch.dict(peers._peers, {"b": "http://127.0.0.1:7880"}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(peers._cache.clear)

    def _poll(self, payload):
        with mock.patch.object(peers, "_request", return_value=(200, payload)):
            peers._poll_once("b", "http://127.0.0.1:7880")

    def test_peer_rows_are_readdressed_and_age_like_its_cards(self):
        self._poll({"windows": [], "gpus": [_row("its-own-label", 0, {"session": None})]})
        (row,) = peers.remote_gpus()
        self.assertEqual((row["host"], row["stale"]), ("b", False))
        peers._cache["b"]["ts"] -= peers.STALE_AFTER + 1
        self.assertTrue(peers.remote_gpus()[0]["stale"])
        peers._cache["b"]["ts"] -= peers.DROP_AFTER
        self.assertEqual(peers.remote_gpus(), [])

    def test_a_peer_whose_sampler_is_stuck_stays_stale(self):
        self._poll({"windows": [], "gpus": [dict(_row("b", 0), stale=True)]})
        self.assertTrue(peers.remote_gpus()[0]["stale"])

    def test_a_board_too_old_to_send_gpus_has_no_rows(self):
        self._poll({"windows": [{"pid": 4}]})
        self.assertEqual(peers.remote_gpus(), [])

    def test_merged_snapshot_names_a_peer_card_on_a_peer_gpu(self):
        local = {"windows": [{"pid": 1, "key": "1", "host": "a", "session_id": "s1",
                              "triage": "completed", "updated_at": 1}],
                 "gpus": [_row("a", 0, {"session": "s2"})],
                 "counts": {}, "ts": 0, "tmux_available": True, "host": "a"}
        remote = [{"pid": 7, "key": "b:7", "host": "b", "session_id": "s2",
                   "triage": "working", "updated_at": 2}]
        with mock.patch.object(appmod, "_local_snapshot", return_value=local), \
             mock.patch.object(peers, "enabled", return_value=True), \
             mock.patch.object(peers, "remote_windows", return_value=remote), \
             mock.patch.object(peers, "remote_gpus", return_value=[_row("b", 3, {"session": "s1"})]), \
             mock.patch.object(peers, "status", return_value=[]):
            snap = appmod._enriched_snapshot()
        self.assertEqual([h["host"] for h in snap["gpus"]], ["a", "b"])
        self.assertEqual({w["key"]: w["gpus"] for w in snap["windows"]}, {"1": ["b:3"], "b:7": ["a:0"]})
        self.assertEqual(len(appmod.state.last_local_snapshot["gpus"]), 1,
                         "the local half handed to peers never grows peer rows")

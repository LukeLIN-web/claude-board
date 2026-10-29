"""Tests for scripts/gpu_wait.py: picking cards, the claim holders, slots, release.

nvidia-smi and the CUDA probe are stubbed; the claims are real: real holder
processes holding real flocks, in a lock directory of each test's own.
"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import gpu_wait

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
CARDS = (0, 1, 2)
OLD_STYLE = ("import fcntl, os, subprocess, sys; fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR); "
             "fcntl.flock(fd, fcntl.LOCK_EX); "
             "print(subprocess.Popen(['sleep', '60'], pass_fds=(fd,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).pid)")


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end
        time.sleep(0.05)


def holders():
    return {card: gpu_wait.claim_holder(gpu_wait.claim_lock(card)) for card in CARDS}


def unclaimed():
    return not any(gpu_wait.held(path) for _, _, path in gpu_wait.claim_locks())


@pytest.fixture
def locks(tmp_path, monkeypatch):
    monkeypatch.setattr(gpu_wait, "LOCK_DIR", str(tmp_path / "locks"))
    os.makedirs(gpu_wait.LOCK_DIR)


@pytest.fixture
def pipes():
    """``life()``: a pipe's read end for claim holders to read, as gpu_wait makes one for its
    job; they all let go when the test ends."""
    ends = []

    def life():
        r, w = os.pipe()
        ends.append(w)
        return r
    yield life
    for w in ends:
        os.close(w)


@pytest.fixture
def cli(tmp_path, locks):
    """``gpu_wait.py`` as a command on a box whose CUDA 0-2 have 40 GB free, with the claim
    locks in the test's LOCK_DIR and no launch to record claims on."""
    noop = tmp_path / "no_gpu_busy.py"
    noop.write_text("")
    script = tmp_path / "gpu_wait_cli.py"
    script.write_text(f"import sys\nsys.path.insert(0, {str(SCRIPTS)!r})\nimport gpu_wait\n"
                      f"gpu_wait.LOCK_DIR = {gpu_wait.LOCK_DIR!r}\ngpu_wait.BUSY = {str(noop)!r}\n"
                      f"gpu_wait.ecc_bad_gpus = set\n"
                      f"gpu_wait.query_free_mb = lambda: {dict.fromkeys(CARDS, 40000)!r}\ngpu_wait.main()\n")
    return [sys.executable, str(script)]


@pytest.fixture
def run(cli):
    """Start a job under gpu_wait on ``num_gpus`` cards; returned once they are claimed."""
    procs = []

    def start(*job, num_gpus=len(CARDS), args=(), **kw):
        p = subprocess.Popen([*cli, "--need-gb", "1", "--num-gpus", str(num_gpus), *args, "--", *job],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True, **kw)
        procs.append(p)
        assert p.stdout.readline().startswith("[gpu_wait] claimed + running on GPU")
        return p
    yield start
    for p in procs:
        p.kill()
        p.wait()


def release(cli, job, *cards):
    return subprocess.run([*cli, "release", str(job), *map(str, cards)], capture_output=True, text=True)


# ── picking cards ──────────────────────────────────────────────────────────────────────


def test_parse_free_mb_maps_index_to_free():
    assert gpu_wait.parse_free_mb("0, 81072\n1, 7553\n2, 52521\n") == {0: 81072, 1: 7553, 2: 52521}
    assert gpu_wait.parse_free_mb("0, 100\n\n  \n1, 200\n") == {0: 100, 1: 200}


def test_free_vram_is_keyed_by_cuda_ordinal_not_smi_index(monkeypatch):
    """A box whose smi index 2 is CUDA-invisible: every higher smi index is one CUDA ordinal
    lower, and the invisible card's free VRAM is out of the pool."""
    smi = "GPU-aaa, 13645\nGPU-bbb, 95320\nGPU-poison, 95320\nGPU-ccc, 1024\nGPU-ddd, 94900\n"
    monkeypatch.setattr(gpu_wait, "_smi", lambda q: smi)
    monkeypatch.setattr(gpu_wait, "cuda_uuids", lambda: {0: "GPU-aaa", 1: "GPU-bbb", 2: "GPU-ccc", 3: "GPU-ddd"})
    assert gpu_wait.query_free_mb() == {0: 13645, 1: 95320, 2: 1024, 3: 94900}


def test_without_a_cuda_enumeration_no_card_is_claimable(monkeypatch):
    monkeypatch.setattr(gpu_wait, "cuda_uuids", dict)
    monkeypatch.setattr(gpu_wait, "_smi", lambda q: "0, 100\n1, 200\n")   # smi indices are never used
    assert gpu_wait.query_free_mb() == {}


def test_the_enumeration_asks_card_by_card_when_the_whole_fails_and_again_when_none_answers(monkeypatch):
    broken = {"all": True, "one": False}

    def probe(code, cvd=None):
        if cvd is None:
            return "" if broken["all"] else "0, GPU-a\n1, GPU-b\n"
        return "" if broken["one"] else f"0, GPU-{'ab'[int(cvd)]}\n"
    monkeypatch.setattr(gpu_wait, "_probe", probe)
    monkeypatch.setattr(gpu_wait, "_smi", lambda q: "0\n1\n")
    monkeypatch.setattr(gpu_wait, "_CUDA_UUIDS", {})
    broken["one"] = True
    assert gpu_wait.cuda_uuids() == {}
    broken["one"] = False
    assert gpu_wait.cuda_uuids() == {0: "GPU-a", 1: "GPU-b"}
    broken["one"] = True
    assert gpu_wait.cuda_uuids() == {0: "GPU-a", 1: "GPU-b"}   # once found, cached


class Clock:   # stands in for gpu_wait.time
    def __init__(self, times):
        self.times = iter(times)

    def monotonic(self):
        return next(self.times)

    def sleep(self, s):
        pass


def test_the_wait_heartbeat_escalates_to_a_stalled_marker(monkeypatch):
    """A silent wait is indistinguishable from progress: past stall_alert_s every
    heartbeat line carries a greppable STALLED."""
    lines = []
    monkeypatch.setattr(gpu_wait, "time", Clock([0.0, 10.0, 4000.0, 8000.0]))
    monkeypatch.setattr(gpu_wait, "claim_free_gpus", lambda *a, **k: None)
    assert gpu_wait.wait_and_claim_gpus(60, interval_s=1, timeout_s=7000, log=lambda m, **k: lines.append(m),
                                        stall_alert_s=3600, cmd=["job"], life=-1) is None
    assert len(lines) == 2 and "STALLED" not in lines[0] and "STALLED" in lines[1]


def test_a_claim_is_exclusive_and_all_or_nothing(locks, pipes, monkeypatch):
    monkeypatch.setattr(gpu_wait, "query_free_mb", lambda: {0: 90000, 1: 90000})
    monkeypatch.setattr(gpu_wait, "ecc_bad_gpus", set)
    a = gpu_wait.claim_free_gpus(60, 1, (), ["a"], pipes())
    assert [card for card, _ in a] == [0]                                     # ties go to the lowest ordinal
    assert gpu_wait.claim_free_gpus(60, 2, (), ["b"], pipes()) is None        # one card left: claims none
    wait_for(lambda: not gpu_wait.held(gpu_wait.claim_lock(1)))
    b = gpu_wait.claim_free_gpus(60, 1, (), ["b"], pipes())
    assert [card for card, _ in b] == [1]
    assert gpu_wait.claim_free_gpus(60, 1, (), ["c"], pipes()) is None        # both claimed: no double load
    for _, holder in a + b:
        gpu_wait.end(holder)
    wait_for(unclaimed)
    assert [card for card, _ in gpu_wait.claim_free_gpus(60, 2, (), ["c"], pipes())] == [0, 1]


def test_a_card_that_lost_its_free_vram_after_the_claim_is_let_go(locks, pipes, monkeypatch):
    """A loader outside gpu_wait can take the card between the scan and the claim."""
    scans = iter([{0: 90000}, {0: 500}])
    monkeypatch.setattr(gpu_wait, "query_free_mb", lambda: next(scans))
    monkeypatch.setattr(gpu_wait, "ecc_bad_gpus", set)
    assert gpu_wait.claim_free_gpus(60, 1, (), ["job"], pipes()) is None
    wait_for(unclaimed)


# ── slots ──────────────────────────────────────────────────────────────────────────────


def as_older_slot_copies_read(card):
    """Whether the first copy with slots takes the card for shared: its reader of slot 0's note."""
    with open(gpu_wait.claim_lock(card)) as f:
        words = f.readline().split()
    return len(words) > 1 and words[1].startswith("slots=") and int(words[1][6:]) > 1


def test_jobs_with_slots_share_a_card_and_a_job_alone_waits_for_all_of_them(locks, pipes):
    a = gpu_wait.claim_card(0, 2, ["sim", "a"], pipes())
    b = gpu_wait.claim_card(0, 2, ["sim", "b"], pipes())
    assert gpu_wait.lock_holder(gpu_wait.claim_lock(0)) == a and gpu_wait.lock_holder(gpu_wait.claim_lock(0, 1)) == b
    assert gpu_wait.read_note(gpu_wait.claim_lock(0)) == (a, 2, "sim a") and as_older_slot_copies_read(0)
    assert gpu_wait.claim_card(0, 2, ["sim", "c"], pipes()) is None   # both slots taken
    assert gpu_wait.claim_card(0, 1, ["alone"], pipes()) is None      # slot 0 is a shared job's
    gpu_wait.end(a)
    wait_for(lambda: not gpu_wait.held(gpu_wait.claim_lock(0)))
    assert gpu_wait.claim_card(0, 1, ["alone"], pipes()) is None      # slot 0 free, but b is still on the card
    wait_for(lambda: not gpu_wait.held(gpu_wait.claim_lock(0)))
    c = gpu_wait.claim_card(0, 2, ["sim", "c"], pipes())
    assert gpu_wait.lock_holder(gpu_wait.claim_lock(0)) == c
    gpu_wait.end(b)
    gpu_wait.end(c)
    wait_for(unclaimed)
    alone = gpu_wait.claim_card(0, 1, ["alone"], pipes())
    assert gpu_wait.read_note(gpu_wait.claim_lock(0)) == (alone, 1, "alone") and not as_older_slot_copies_read(0)
    assert gpu_wait.claim_card(0, 2, ["sim", "d"], pipes()) is None   # the card is one job's
    assert not gpu_wait.held(gpu_wait.claim_lock(0, 1))


def test_a_slot_job_takes_the_next_slot_of_a_card_an_older_copy_shares(locks, pipes):
    """The first copy with slots holds the lock in its wrapper and writes ``slots=K`` into every slot it takes."""
    path = gpu_wait.claim_lock(0)
    old = subprocess.Popen([sys.executable, "-c", "import fcntl, os, sys, time; fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR); "
                            "fcntl.flock(fd, fcntl.LOCK_EX); os.write(fd, f'pid={os.getpid()} slots=3 python sim.py\\n'.encode()); "
                            "print('held', flush=True); time.sleep(60)", path], stdout=subprocess.PIPE, text=True)
    try:
        assert old.stdout.readline() == "held\n"
        assert gpu_wait.read_note(path) == (old.pid, 3, "python sim.py")
        mine = gpu_wait.claim_card(0, 3, ["sim"], pipes())
        assert gpu_wait.lock_holder(gpu_wait.claim_lock(0, 1)) == mine
        assert gpu_wait.claim_card(0, 1, ["alone"], pipes()) is None
    finally:
        old.kill()
        old.wait()


def test_release_finds_the_jobs_own_slot(cli, run):
    one = ("--slots", "2", "--exclude", "1,2")
    a = run("sleep", "60", num_gpus=1, args=one)
    b = run("sleep", "60", num_gpus=1, args=one)   # slot 1 of CUDA 0
    slot0, slot1 = gpu_wait.claim_lock(0), gpu_wait.claim_lock(0, 1)
    held_by_a = gpu_wait.claim_holder(slot0)
    assert release(cli, b.pid, 0).returncode == 0
    assert gpu_wait.claim_holder(slot1) is None and gpu_wait.claim_holder(slot0) == held_by_a
    r = release(cli, b.pid, 0)
    assert r.returncode == 1 and "GPU 0 is claimed for another job (sleep 60)" in r.stderr
    assert release(cli, a.pid, 0).returncode == 0 and gpu_wait.claim_holder(slot0) is None


def test_hold_is_for_a_card_of_ones_own(cli):
    r = subprocess.run([*cli, "--slots", "2", "--hold", "--", "true"], capture_output=True, text=True)
    assert r.returncode == 2 and "not with --slots" in r.stderr


# ── the claim holders ──────────────────────────────────────────────────────────────────


def test_release_frees_the_cards_named_and_keeps_the_rest_for_the_job(cli, run):
    job = run("sleep", "60")
    claimed = holders()
    assert None not in claimed.values()
    r = release(cli, job.pid, 1)
    assert r.returncode == 0 and r.stdout == "[gpu_wait] released GPU 1 (claimed for: sleep 60)\n"
    assert holders() == {**claimed, 1: None} and gpu_wait.lock_holder(gpu_wait.claim_lock(1)) is None
    assert job.poll() is None
    job.kill()
    job.wait()
    wait_for(unclaimed)


def test_the_claim_lives_while_any_process_of_the_job_does(run):
    job = run("sh", "-c", "sleep 60 & echo $!; wait", num_gpus=1)
    heir = int(job.stdout.readline())
    claimed = holders()
    job.kill()   # its top process, which gpu_wait was, dies; its child runs on
    job.wait()
    try:
        assert holders() == claimed and claimed[0] is not None
    finally:
        os.kill(heir, 9)
    wait_for(unclaimed)


@pytest.mark.parametrize("caller", [None, lambda: signal.signal(gpu_wait.END, signal.SIG_IGN),
                                    lambda: signal.pthread_sigmask(signal.SIG_BLOCK, {gpu_wait.END})],
                         ids=["plain", "caller ignores END", "caller blocks END"])
def test_a_wait_that_falls_short_keeps_no_claim(cli, caller):
    p = subprocess.Popen([*cli, "--need-gb", "1", "--num-gpus", "4", "--interval-min", "1", "--", "true"],
                         stdout=subprocess.PIPE, text=True, preexec_fn=caller)
    try:
        assert "no 4 claimable card(s)" in p.stdout.readline()
        wait_for(unclaimed)
        assert p.poll() is None
    finally:
        p.kill()
        p.wait()


def test_release_refuses_claims_no_claim_holder_holds_and_releases_none(cli, run):
    old = subprocess.run([sys.executable, "-c", OLD_STYLE, gpu_wait.claim_lock(1)], capture_output=True, text=True, check=True)
    heir = int(old.stdout)   # an old-style claim: its job holds the lock fd, the gpu_wait that took it is gone
    try:
        job = run("sleep", "60", num_gpus=1)
        claimed = holders()
        r = release(cli, job.pid, 0, 1, 2)
        assert r.returncode == 1 and r.stdout == ""
        assert "GPU 1: its claim lock is held by pid " in r.stderr and "(exited)" in r.stderr
        assert "GPU 2 is not claimed" in r.stderr
        assert holders() == claimed and claimed[0] is not None
    finally:
        os.kill(heir, 9)


def test_the_claim_holder_says_whose_job_it_is(tmp_path, run):
    job = run("sleep", "60", num_gpus=1, cwd=tmp_path, env=dict(os.environ, CLAUDE_CODE_SESSION_ID="session-1"))
    holder = gpu_wait.claim_holder(gpu_wait.claim_lock(0))
    assert Path(gpu_wait.claim_lock(0)).read_text() == f"pid={holder} sleep 60\n"
    assert gpu_wait.argv_of(holder)[4:] == ["1", "--", "sleep", "60"]
    assert b"CLAUDE_CODE_SESSION_ID=session-1" in Path(f"/proc/{holder}/environ").read_bytes().split(b"\0")
    assert os.readlink(f"/proc/{holder}/cwd") == str(tmp_path)
    wait_for(lambda: gpu_wait.argv_of(job.pid) == ["sleep", "60"])   # gpu_wait became the job
    assert b"CUDA_VISIBLE_DEVICES=0" in Path(f"/proc/{job.pid}/environ").read_bytes().split(b"\0")
    assert len(os.listdir(f"/proc/{job.pid}/fd")) == 4   # stdio and the pipe the holder reads
    assert Path(f"/proc/{holder}/stat").read_text().rpartition(")")[2].split()[1] != str(job.pid)


def test_release_refuses_a_card_claimed_for_another_job(cli, run):
    mine = run("sleep", "60", num_gpus=1)
    other = run("sleep", "60", num_gpus=1)   # CUDA 1
    claimed = holders()
    r = release(cli, mine.pid, 1)
    assert r.returncode == 1 and "GPU 1 is claimed for another job (sleep 60)" in r.stderr
    assert holders() == claimed
    assert release(cli, other.pid, 1).returncode == 0 and holders() == {**claimed, 1: None}


def test_a_term_sweep_that_reaches_the_claim_holder_keeps_the_claim(cli, run):
    job = run("sleep", "60", num_gpus=1)
    holder = holders()[0]
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        os.kill(holder, sig)
    time.sleep(0.5)
    assert holders()[0] == holder
    job.kill()
    job.wait()
    wait_for(unclaimed)

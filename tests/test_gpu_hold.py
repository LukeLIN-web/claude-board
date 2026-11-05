import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import gpu_hold
import gpu_wait

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
CARD = 3


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end
        time.sleep(0.05)


@pytest.fixture(autouse=True)
def cli(tmp_path, monkeypatch):
    """``gpu_hold.py`` as a command, with the hold locks in a scratch HOLD_DIR and the claim locks in a scratch LOCK_DIR."""
    monkeypatch.setattr(gpu_hold, "HOLD_DIR", str(tmp_path / "hold"))
    monkeypatch.setattr(gpu_wait, "LOCK_DIR", str(tmp_path / "locks"))
    os.makedirs(gpu_hold.HOLD_DIR)
    os.makedirs(gpu_wait.LOCK_DIR)
    script = tmp_path / "gpu_hold_cli.py"
    script.write_text(f"import sys\nsys.path.insert(0, {str(SCRIPTS)!r})\nimport gpu_hold\n"
                      f"gpu_hold.HOLD_DIR = {gpu_hold.HOLD_DIR!r}\ngpu_hold.main()\n")
    return [sys.executable, str(script)]


def test_use_waits_for_the_ballast_and_keeps_the_card_while_running(cli):
    path = gpu_hold.lock_path(CARD)
    ballast = gpu_hold.try_lock(path)
    assert ballast is not None
    job = subprocess.Popen([*cli, "use", str(CARD), "--", sys.executable,
                            "-c", "import time; print('started', flush=True); time.sleep(30)"],
                           stdout=subprocess.PIPE, text=True)
    try:
        wait_for(lambda: (True, job.pid) in gpu_hold.flocks(path))
        os.close(ballast)
        assert job.stdout.readline() == "started\n"
        assert gpu_hold.flocks(path) == [(False, job.pid)]
        assert gpu_hold.try_lock(path) is None
    finally:
        job.kill()
        job.wait()
    assert gpu_hold.flocks(path) == []


def test_hold_outlives_a_missed_read_of_the_claim(monkeypatch):
    reads = [True, False, True] + [False] * gpu_hold.CLAIM_MISSES
    seen = []
    monkeypatch.setattr(gpu_hold, "claim_alive", lambda card, pid: seen.append(reads[len(seen)]) or seen[-1])
    monkeypatch.setattr(gpu_hold, "card_idle", lambda smi_id: False)
    monkeypatch.setattr(gpu_hold, "TICK_S", 0.0)
    gpu_hold.hold(CARD, os.getpid(), "none")
    assert seen == reads   # a lone missed read does not end the hold; CLAIM_MISSES in a row do


def test_the_claim_lives_for_hold_until_its_card_is_released():
    life, heir = os.pipe()   # as gpu_wait leaves its job the pipe the claim holders read
    try:
        holder = gpu_wait.try_claim(CARD, ["job"], life)
        os.close(life)
        assert gpu_hold.claim_alive(CARD, holder)
        gpu_wait.release(os.getpid(), [CARD])
        assert not gpu_hold.claim_alive(CARD, holder)
    finally:
        os.close(heir)


NOW = 1000.0


def fill_band(monkeypatch):   # with headroom 4096: free kept within 4096..6144 MiB, mid-band 5120
    monkeypatch.setattr(gpu_hold, "CHUNK_MB", 256)
    monkeypatch.setattr(gpu_hold, "FILL_BAND_MB", 2048)


def stable_window(monkeypatch):
    monkeypatch.setattr(gpu_hold, "FILL_STABLE_S", 60.0)
    monkeypatch.setattr(gpu_hold, "FILL_STABLE_MB", 512)


def test_fill_gives_back_enough_chunks_to_reach_mid_band_when_free_falls_below_headroom(monkeypatch):
    fill_band(monkeypatch)
    assert gpu_hold.fill_step(3000, 100, 4096) == -9   # 3000 + 8 * 256 falls short of 5120; 3000 + 9 * 256 does not


def test_fill_gives_back_no_more_than_it_holds(monkeypatch):
    fill_band(monkeypatch)
    assert gpu_hold.fill_step(3000, 4, 4096) == -4
    assert gpu_hold.fill_step(3000, 0, 4096) == 0


def test_fill_takes_chunks_down_to_mid_band_when_free_is_above_the_band(monkeypatch):
    fill_band(monkeypatch)
    assert gpu_hold.fill_step(10000, 0, 4096) == 19   # 10000 - 19 * 256 = 5136: stops short of going under 5120


def test_fill_leaves_free_vram_alone_inside_the_band(monkeypatch):
    fill_band(monkeypatch)
    for free in (4096, 5120, 6144):
        assert gpu_hold.fill_step(free, 10, 4096) == 0


def test_not_settled_without_samples_in_the_window(monkeypatch):
    stable_window(monkeypatch)
    assert not gpu_hold.settled([], NOW)
    assert not gpu_hold.settled([(NOW - 120, 30000)], NOW)


def test_not_settled_before_samples_span_the_window(monkeypatch):
    stable_window(monkeypatch)
    assert not gpu_hold.settled([(NOW - 55 + 5 * i, 30000) for i in range(12)], NOW)


def test_settled_when_used_vram_moves_within_the_limit_over_the_window(monkeypatch):
    stable_window(monkeypatch)
    assert gpu_hold.settled([(NOW - 60 + 5 * i, 30000 + 400 * (i % 2)) for i in range(13)], NOW)


def test_not_settled_when_used_vram_moves_past_the_limit_in_the_window(monkeypatch):
    stable_window(monkeypatch)
    samples = [(NOW - 60 + 5 * i, 30000) for i in range(12)] + [(NOW, 30600)]
    assert not gpu_hold.settled(samples, NOW)


def test_settled_ignores_moves_before_the_window(monkeypatch):
    stable_window(monkeypatch)
    samples = [(NOW - 64, 10000)] + [(NOW - 59 + 5 * i, 30000) for i in range(12)]
    assert gpu_hold.settled(samples, NOW)


class Clock:   # stands in for gpu_hold.time: sleep moves the clock at once
    strftime = staticmethod(time.strftime)

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, dt):
        self.now += dt


class Child:   # a ballast or fill that runs until terminated
    def __init__(self, clock, args):
        self.clock, self.args, self.at, self.stopped, self.waits = clock, args, clock.now, [], 0

    def poll(self):
        return None

    def terminate(self):
        self.stopped.append(self.clock.now)

    def wait(self):
        self.waits += 1


def run_hold(monkeypatch, job_on, claim_until, fill_mb=4096, used_mb=lambda now: 30000, idle=False,
             bridge_on=lambda now: False):
    """``hold`` on CARD against a fake clock: the job holds the lock while ``job_on(now)``, a ballast while it
    runs, a bridge while ``bridge_on(now)``, and the claim lives until ``claim_until``. Returns the children
    started and the times used VRAM was read."""
    clock, children, reads = Clock(), [], []

    def spawn(smi_id, *args, pass_fds=()):
        children.append(Child(clock, args))
        return children[-1]

    def flocks(path):
        bridge = [(False, 4444)] if bridge_on(clock.now) else []
        if path == gpu_hold.bridge_lock(CARD):
            return bridge
        ballasts = [(False, 4343) for c in children if c.args[0] == "ballast" and not c.stopped]
        return ([(False, 4242)] if job_on(clock.now) else []) + ballasts + bridge

    def card_used_mb(smi_id):
        reads.append(clock.now)
        return used_mb(clock.now)

    monkeypatch.setattr(gpu_hold, "time", clock)
    monkeypatch.setattr(gpu_hold, "spawn", spawn)
    monkeypatch.setattr(gpu_hold, "flocks", flocks)
    monkeypatch.setattr(gpu_hold, "card_used_mb", card_used_mb)
    monkeypatch.setattr(gpu_hold, "card_idle", lambda smi_id: idle)
    monkeypatch.setattr(gpu_hold, "try_lock", lambda path: -1)
    monkeypatch.setattr(gpu_hold, "claim_alive", lambda card, pid: clock.now < claim_until)
    gpu_hold.hold(CARD, os.getpid(), "none", fill_mb)
    return children, reads


def test_hold_without_fill_mb_neither_fills_nor_reads_used_vram(monkeypatch):
    children, reads = run_hold(monkeypatch, lambda now: True, 3 * gpu_hold.FILL_AFTER_S, fill_mb=None)
    assert children == [] and reads == []


def test_fill_starts_once_the_job_has_been_on_the_card_fill_after_s_and_settled(monkeypatch):
    came_on = 5.0
    children, _ = run_hold(monkeypatch, lambda now: now >= came_on, came_on + gpu_hold.FILL_AFTER_S + 60, fill_mb=6000)
    [fill] = children
    assert fill.args == ("fill", str(CARD), str(os.getpid()), "6000")
    assert fill.at == came_on + gpu_hold.FILL_AFTER_S


def test_a_bridge_on_the_card_is_not_the_job(monkeypatch):
    """The fill waits FILL_AFTER_S from the job's own ``use``, not from the bridge that covered the card before it."""
    came_on = 100.0
    children, _ = run_hold(monkeypatch, lambda now: now >= came_on, came_on + gpu_hold.FILL_AFTER_S + 60,
                           bridge_on=lambda now: now < came_on + 1)
    [fill] = children
    assert fill.args[0] == "fill" and fill.at == came_on + gpu_hold.FILL_AFTER_S


def test_no_fill_while_used_vram_keeps_moving(monkeypatch):
    def moving(now):   # each sample is 2 * FILL_STABLE_MB off the one before
        return 30000 + 2 * gpu_hold.FILL_STABLE_MB * int(now // gpu_hold.FILL_SAMPLE_S % 2)
    children, _ = run_hold(monkeypatch, lambda now: True, 3 * gpu_hold.FILL_AFTER_S, used_mb=moving)
    assert children == []


def test_fill_outlasts_a_restart_between_tasks_and_gives_way_to_the_ballast_once_the_job_leaves(monkeypatch):
    restart = gpu_hold.FILL_AFTER_S + 30   # the lock is empty for 1 s
    left = gpu_hold.FILL_AFTER_S + 60
    children, _ = run_hold(monkeypatch, lambda now: now < restart or restart + 1 <= now < left, left + 30, idle=True)
    fill, ballast = children
    assert fill.args[0] == "fill" and fill.at == gpu_hold.FILL_AFTER_S
    [stopped] = fill.stopped
    assert left < stopped <= left + gpu_hold.FILL_DROP_S
    assert ballast.args[0] == "ballast" and ballast.at == stopped + gpu_hold.TICK_S


def test_hold_stops_the_fill_when_the_claim_ends(monkeypatch):
    claim_end = gpu_hold.FILL_AFTER_S + 30
    children, _ = run_hold(monkeypatch, lambda now: True, claim_end)
    [fill] = children
    assert fill.stopped == [claim_end + gpu_hold.CLAIM_MISSES * gpu_hold.TICK_S] and fill.waits == 1


# ── the bridge between two claims ────────────────────────────────────────────────────


def run_bridge(monkeypatch, free_mb, claimed=lambda now: False, idle=(True, True), clearing=False):
    """``bridge`` on CARD against a fake clock: the card's free VRAM is ``free_mb(now)``, a claim holds
    it while ``claimed(now)``, ``idle`` is what nvidia-smi shows on each look before the context, then
    after it, and every process it lists is exiting when ``clearing``.
    Returns what it wrote to the up fd, when it left, the contexts it opened, and the hold and
    bridge locks' flocks while it was up."""
    clock, contexts, idle, locks = Clock(), [], iter(idle), []

    def look(smi_id, mine=None):
        assert clock.now < 10 * gpu_hold.BRIDGE_GRACE_S, "the bridge never gave up"
        return next(idle)

    def free(cuda, card):
        assert clock.now < 10 * gpu_hold.BRIDGE_GRACE_S, "the bridge never left"
        if not locks:
            locks.append([gpu_hold.flocks(path) for path in (gpu_hold.lock_path(CARD), gpu_hold.bridge_lock(CARD))])
        return free_mb(clock.now)

    monkeypatch.setattr(gpu_hold, "time", clock)
    monkeypatch.setattr(gpu_hold, "BRIDGE_TICK_S", 0.5)
    monkeypatch.setattr(gpu_hold, "ctypes", SimpleNamespace(CDLL=lambda name: SimpleNamespace(prctl=lambda *a: 0)))
    monkeypatch.setattr(gpu_hold, "visible_uuid", lambda: "GPU-c")
    monkeypatch.setattr(gpu_hold, "card_idle", look)
    monkeypatch.setattr(gpu_hold, "card_clearing", lambda smi_id: clearing)
    monkeypatch.setattr(gpu_hold, "cuda_on_card", lambda card: contexts.append(card))
    monkeypatch.setattr(gpu_hold, "card_free_mb", free)
    monkeypatch.setattr(gpu_hold, "held_claims", lambda: [(CARD, "lock", 4242)] if claimed(clock.now) else [])
    r, w = os.pipe()
    before = set(os.listdir("/proc/self/fd"))
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    try:
        gpu_hold.bridge(CARD, os.getppid(), w)
    finally:   # it resets them, as a process of its own should; this one is pytest
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
    opened = [int(fd) for fd in set(os.listdir("/proc/self/fd")) - before
              if os.path.realpath(f"/proc/self/fd/{fd}") in (gpu_hold.lock_path(CARD), gpu_hold.bridge_lock(CARD))]
    for fd in [w, *opened]:   # the up fd, unless it closed it, and the locks it holds until it exits
        with contextlib.suppress(OSError):
            os.close(fd)
    with os.fdopen(r, "rb") as up:
        return up.read(), clock.now, contexts, locks


def test_a_bridge_covers_the_card_until_a_new_context_comes_on(monkeypatch):
    def free_mb(now):   # the last job's context is still going at first; a new one comes on at 5 s
        return 46000 if now < 2 else 47000 if now < 5 else 47000 - 2 * gpu_hold.BRIDGE_DROP_MB
    up, left, contexts, [[hold, bridge]] = run_bridge(monkeypatch, free_mb, claimed=lambda now: now >= 1)
    assert up == b"up" and left == 5.0 and contexts == [CARD]
    assert hold == [(False, os.getpid())] and bridge == [(False, os.getpid())]


def test_a_bridge_no_claim_takes_leaves_after_the_grace(monkeypatch):
    up, left, _, _ = run_bridge(monkeypatch, lambda now: 47000, claimed=lambda now: now <= 3)
    assert up == b"up" and left == 3 + gpu_hold.BRIDGE_GRACE_S


@pytest.mark.parametrize("idle, other_bridge, opened", [((False,), False, []), ((True,), True, []),
                                                        ((True, False), False, [CARD])],
                         ids=["card in use", "another bridge covers it", "somebody came on before it read free VRAM"])
def test_a_bridge_stays_off(monkeypatch, idle, other_bridge, opened):
    other = gpu_hold.try_lock(gpu_hold.bridge_lock(CARD)) if other_bridge else None
    try:
        up, _, contexts, _ = run_bridge(monkeypatch, lambda now: 47000, idle=idle)
    finally:
        if other is not None:
            os.close(other)
    assert up == b"" and contexts == opened


def test_a_bridge_looks_at_the_card_once_a_ballast_has_let_go(monkeypatch):
    """The bridge waits on the card's lock, which a ``hold`` sees: it lets its ballast go, and the
    claim does not end with the ballast on the card. The bridge takes its bridge lock only after
    that wait: ``hold`` leaves the bridge lock's holders out of the waiters, and would keep the ballast."""
    ballast, let_go, seen, bridges = gpu_hold.try_lock(gpu_hold.lock_path(CARD)), [], [], []

    def hold():   # what `hold` does when somebody waits on the lock
        try:
            wait_for(lambda: any(waiting for waiting, _ in gpu_hold.flocks(gpu_hold.lock_path(CARD))))
            bridges.append(gpu_hold.flocks(gpu_hold.bridge_lock(CARD)))
            let_go.append(True)
        finally:
            os.close(ballast)

    def idle():
        seen.append(bool(let_go))
        yield False
    hold_thread = threading.Thread(target=hold)
    hold_thread.start()
    run_bridge(monkeypatch, lambda now: 47000, idle=idle())
    hold_thread.join()
    assert seen == [True] and bridges == [[]]


@pytest.mark.parametrize("apps, used, mine, idle", [("", "300", None, True), ("77", "300", None, False),
                                                    ("77", "300", 77, True), ("77\n78", "300", 77, False),
                                                    ("", "2000", None, False), ("", "300", 77, False)])
def test_a_card_is_idle_with_no_process_but_mine_and_little_used(monkeypatch, apps, used, mine, idle):
    monkeypatch.setattr(gpu_hold, "smi", lambda smi_id, what, field: (apps if what == "compute-apps" else used).split())
    assert gpu_hold.card_idle("GPU-c", mine) is idle


def test_a_bridge_waits_while_the_last_jobs_processes_finish_exiting(monkeypatch):
    up, _, contexts, _ = run_bridge(monkeypatch, lambda now: 47000, idle=(False, False, True, True), clearing=True)
    assert up == b"up" and contexts == [CARD]


def test_a_bridge_gives_up_on_a_card_that_does_not_clear(monkeypatch):
    up, left, contexts, _ = run_bridge(monkeypatch, lambda now: 47000, idle=iter(lambda: False, None), clearing=True)
    assert up == b"" and contexts == [] and left >= gpu_hold.BRIDGE_CLEAR_S


def test_a_card_is_clearing_only_while_every_listed_process_is_exiting(monkeypatch):
    listed = []
    monkeypatch.setattr(gpu_hold, "smi", lambda smi_id, what, field: listed)
    exiting = subprocess.Popen(["true"])   # left unreaped: a zombie, as an exiting process looks
    try:
        wait_for(lambda: gpu_wait.exited(exiting.pid))
        listed[:] = [str(exiting.pid)]
        assert gpu_hold.card_clearing("GPU-c")
        listed[:] = []
        assert gpu_hold.card_clearing("GPU-c")   # only memory left to free
        listed[:] = [str(exiting.pid), str(os.getpid())]
        assert not gpu_hold.card_clearing("GPU-c")   # this process is alive
        listed[:] = ["[N/A]"]
        assert not gpu_hold.card_clearing("GPU-c")   # another pid namespace's: cannot tell
    finally:
        exiting.wait()


def test_the_bridge_command_runs_up_to_its_parent_check():
    """``gpu_hold.py bridge`` as start_bridge runs it (under BALLAST_NAME, with PYTHONHOME, its
    arguments parsed), given a pid that is not its parent's: it leaves before any CUDA call."""
    up, up_w = os.pipe()
    proc = gpu_hold.spawn(str(CARD), "bridge", str(CARD), str(os.getpid() + 1), str(up_w), pass_fds=(up_w,),
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    os.close(up_w)
    try:
        assert proc.wait(timeout=30) == 0, proc.stderr.read()
        assert os.read(up, 2) == b""
    finally:
        os.close(up)
        proc.stderr.close()


@pytest.mark.parametrize("code, ready, rc", [("os.write(int(sys.argv[1]), b'up'); time.sleep(30)", True, None),
                                             ("pass", False, 0), ("time.sleep(30)", False, -9)],
                         ids=["up", "stays off", "not up in time"])
def test_start_bridge_waits_for_the_bridge(monkeypatch, tmp_path, code, ready, rc):
    """The bridge, and the nvidia-smi it may be running when it is killed, are gone unless it is up."""
    procs, child = [], tmp_path / "child"

    def spawn(smi_id, *args, pass_fds=(), **popen):
        script = ("import os, subprocess, sys, time\n"
                  f"open(sys.argv[2], 'w').write(str(subprocess.Popen(['sleep', '30']).pid))\n{code}")
        procs.append(subprocess.Popen([sys.executable, "-c", script, args[-1], str(child)], pass_fds=pass_fds, **popen))
        return procs[-1]
    monkeypatch.setattr(gpu_hold, "spawn", spawn)
    try:
        assert gpu_hold.start_bridge(CARD, time.monotonic() + 2.0) is ready
        assert procs[0].poll() == rc
        if rc == -9:
            wait_for(lambda: gpu_wait.exited(int(child.read_text())))
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(procs[0].pid, signal.SIGKILL)
        procs[0].wait()

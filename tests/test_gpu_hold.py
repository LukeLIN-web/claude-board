import os
import subprocess
import sys
import time
from pathlib import Path

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


def run_hold(monkeypatch, job_on, claim_until, fill_mb=4096, used_mb=lambda now: 30000, idle=False):
    """``hold`` on CARD against a fake clock: the job holds the lock while ``job_on(now)``, a ballast while it
    runs, and the claim lives until ``claim_until``. Returns the children started and the times used VRAM was read."""
    clock, children, reads = Clock(), [], []

    def spawn(smi_id, *args, pass_fds=()):
        children.append(Child(clock, args))
        return children[-1]

    def flocks(path):
        ballasts = [(False, 4343) for c in children if c.args[0] == "ballast" and not c.stopped]
        return ([(False, 4242)] if job_on(clock.now) else []) + ballasts

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

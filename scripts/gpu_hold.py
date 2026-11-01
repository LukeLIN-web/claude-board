"""Keep a card that gpu_wait claimed from looking idle while none of the job's
processes is on it.

People outside the project do not see gpu_wait claims; they judge a card by the
memory nvidia-smi shows. While services load, between batches, between arms and
at the tail, every process of ours on a claimed card has exited or not yet
allocated, the card reads empty, and an outside job lands on it.
``gpu_wait.py --hold`` starts one ``hold`` per claimed card. Whenever the card
is idle, ``hold`` starts a ballast that holds all its free VRAM but ``RESERVE_MB``
under the name ``BALLAST_NAME`` (what nvidia-smi lists); the ballast is gone
before any process of the job starts on the card.

The handshake is one flock per card, ``HOLD_DIR/gpu<k>`` (``k`` the CUDA
ordinal, as in the claim lock):

* ``use k -- cmd`` takes a shared lock, waiting while the ballast holds the card,
  and execs ``cmd``, which keeps the lock, with every child that inherits the fd
  (Python's subprocess closes it), for its lifetime;
* the ballast holds the exclusive lock; when a waiter shows in /proc/locks,
  ``hold`` kills the ballast and lets the lock go once the ballast has exited;
* ``hold`` arms only when nobody holds or waits on the lock and nvidia-smi shows
  the card empty, so the ballast never takes memory from a process already there.
  A process that skips ``use`` is not protected: run only jobs that start every
  GPU process through ``use`` under ``--hold``.

Outside launchers pick the card with the most free VRAM, so they also land beside
a running batch that leaves room on its card. ``hold --fill-mb H`` (off by
default; ``gpu_wait.py --fill-headroom-mb``) covers that room too: once the job
has been on the card ``FILL_AFTER_S`` (a fill while vLLM sizes its KV cache
shrinks it) and the card's used VRAM has moved by at most ``FILL_STABLE_MB`` over
``FILL_STABLE_S``, a ``fill`` under ``BALLAST_NAME`` keeps the card's free VRAM
between ``H`` and ``H + FILL_BAND_MB`` MiB, giving memory back as the job
allocates. The fill holds no lock, so ``use`` gets the card at once. It is
dropped once nobody has held the lock for ``FILL_DROP_S``, and the idle-card
ballast takes over from there as before. Before restarting its services on the
card (the next batch), a job leaves the lock empty for ``FILL_DROP_S + TICK_S``,
or the fill stays on into the next batch; a slot restarting Isaac between tasks
leaves it empty for under a second, and the fill stays.

``hold`` lives as long as the claim: it stops once the kernel no longer lists
``claim_pid``, the process gpu_wait names as holder of the card's claim lock, in
``CLAIM_MISSES`` reads in a row (the job ended, the card was released with
``gpu_wait.py release``, or the chain was killed); a ballast or fill whose ``hold``
dies gets SIGTERM from the kernel.
"""
from __future__ import annotations

import argparse
import ctypes
import fcntl
import os
import signal
import subprocess
import sys
import time

from gpu_wait import claim_lock, flocks

HOLD_DIR = "/tmp/gpu_hold"   # box-local like LOCK_DIR: flock on NFS is a lie
BALLAST_NAME = "agent-harness:gpu_hold"   # what nvidia-smi lists; the name every copy of gpu_busy knows a ballast by
TICK_S = 0.5
RECHECK_S = 10.0             # after the card was in someone else's use, or the ballast died
IDLE_MB = 1024               # graphics-only processes use memory without showing in compute-apps
RESERVE_MB = 1024            # left free beside the ballast
CHUNK_MB = 256
CLAIM_MISSES = 10            # /proc/locks comes a page per read: a line can slip between pages while other locks come and go
PR_SET_PDEATHSIG = 1
FILL_AFTER_S = 240.0         # no fill sooner after the job came on: vLLM takes up to ~200 s to size its KV cache, flat for a minute after compiling
FILL_STABLE_S = 30.0         # fill once used VRAM has moved by at most FILL_STABLE_MB over this long
FILL_STABLE_MB = 512
FILL_SAMPLE_S = 5.0          # hold samples used VRAM (nvidia-smi) this often
FILL_BAND_MB = 2048          # fill keeps the card's free VRAM within [headroom, headroom + FILL_BAND_MB]
FILL_TICK_S = 0.1            # fill's control period
FILL_DROP_S = 2.0            # the job has left once the lock stays empty this long: before restarting its services (the next batch),
                             # a job leaves it empty FILL_DROP_S + TICK_S, or the fill stays on; Isaac restarting between tasks: under 1 s


def log(card: int, msg: str) -> None:
    print(f"[gpu_hold] {time.strftime('%H:%M:%S')} GPU {card}: {msg}", file=sys.stderr, flush=True)


def lock_path(card: int) -> str:
    return os.path.join(HOLD_DIR, f"gpu{card}")


def claim_alive(card: int, claim_pid: int) -> bool:
    """The kernel still lists ``claim_pid`` as holder of card's claim lock: the claim lives."""
    return (False, claim_pid) in flocks(claim_lock(card))


def smi(smi_id: str, what: str, field: str) -> list[str]:
    return subprocess.check_output(["nvidia-smi", "-i", smi_id, f"--query-{what}={field}",
                                    "--format=csv,noheader,nounits"], text=True, timeout=30).split()


def card_idle(smi_id: str) -> bool:
    """nvidia-smi shows nothing on the card; an unanswered query is not idle."""
    try:
        return not smi(smi_id, "compute-apps", "pid") and int(smi(smi_id, "gpu", "memory.used")[0]) <= IDLE_MB
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return False


def card_used_mb(smi_id: str) -> int | None:
    try:
        return int(smi(smi_id, "gpu", "memory.used")[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def fill_step(free_mb: int, held: int, headroom_mb: int) -> int:
    """Chunks fill takes (> 0) or gives back (< 0, at most ``held``) to bring free VRAM to mid-band."""
    if headroom_mb <= free_mb <= headroom_mb + FILL_BAND_MB:
        return 0
    return max((free_mb - headroom_mb - FILL_BAND_MB // 2) // CHUNK_MB, -held)


def settled(samples: list[tuple[float, int]], now: float) -> bool:
    """``samples`` (``(monotonic time, used MiB)``, oldest first) cover the last ``FILL_STABLE_S``
    and move by at most ``FILL_STABLE_MB`` within it."""
    start = now - FILL_STABLE_S
    window = [mb for t, mb in samples if t >= start]
    return bool(window) and samples[0][0] <= start and max(window) - min(window) <= FILL_STABLE_MB


def try_lock(path: str) -> int | None:
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def spawn(smi_id: str, *args: str, pass_fds: tuple[int, ...] = ()) -> subprocess.Popen:
    """Run this script with ``args`` on the one card ``smi_id``, listed by nvidia-smi as ``BALLAST_NAME``."""
    return subprocess.Popen(
        [BALLAST_NAME, os.path.abspath(__file__), *args], executable=sys.executable, pass_fds=pass_fds,
        # a venv interpreter run under another argv[0] cannot find its stdlib without PYTHONHOME
        env=dict(os.environ, CUDA_VISIBLE_DEVICES=smi_id, PYTHONHOME=sys.base_prefix))


def hold(card: int, claim_pid: int, smi_id: str, fill_mb: int | None = None) -> None:
    os.makedirs(HOLD_DIR, exist_ok=True)
    path = lock_path(card)
    os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o666))
    proc, fd, next_arm, misses = None, -1, 0.0, 0
    filler, last_user, busy_since, samples, next_sample = None, float("-inf"), None, [], 0.0
    while misses < CLAIM_MISSES:
        misses = 0 if claim_alive(card, claim_pid) else misses + 1
        users = flocks(path)
        now = time.monotonic()
        job = proc is None and bool(users)   # while the ballast is armed, its own lock is in users
        if job:
            last_user = now
            if busy_since is None:
                busy_since = now
        if proc is not None:
            rc = proc.poll()
            waiters = [pid for waiting, pid in users if waiting]
            if rc is not None or waiters:
                proc.terminate()
                proc.wait()
                os.close(fd)
                proc = None
                if rc is None:
                    log(card, f"released for pid {waiters[0]}")
                else:
                    log(card, f"ballast exited with rc {rc}; arming again in {RECHECK_S:g} s")
                    next_arm = time.monotonic() + RECHECK_S
        elif filler is not None:
            rc = filler.poll()
            if rc is not None or now - last_user >= FILL_DROP_S:
                filler.terminate()
                filler.wait()
                filler, busy_since, samples = None, None, []
                log(card, "fill stopped" if rc is None else f"fill exited with rc {rc}")
        elif job and fill_mb is not None:
            if now >= next_sample:
                next_sample = now + FILL_SAMPLE_S
                if (used := card_used_mb(smi_id)) is not None:
                    samples.append((now, used))
                samples = [s for s in samples if s[0] >= now - FILL_STABLE_S - FILL_SAMPLE_S]
            if now - busy_since >= FILL_AFTER_S and settled(samples, now):
                log(card, f"job on the card for {now - busy_since:.0f} s and settled; starting fill")
                filler = spawn(smi_id, "fill", str(card), str(os.getpid()), str(fill_mb))
        elif not users and time.monotonic() >= next_arm:
            if not card_idle(smi_id):
                next_arm = time.monotonic() + RECHECK_S
            elif (fd := try_lock(path)) is not None:
                proc = spawn(smi_id, "ballast", str(card), str(os.getpid()), pass_fds=(fd,))
        if not job and now - last_user >= FILL_DROP_S:
            busy_since, samples = None, []
        time.sleep(TICK_S)
    for p in (proc, filler):
        if p is not None:
            p.terminate()
            p.wait()


def cuda_on_card(card: int) -> ctypes.CDLL:
    """libcuda with the one visible card's primary context current; exits if CUDA does not come up."""
    cuda = ctypes.CDLL("libcuda.so.1")
    dev, ctx = ctypes.c_int(), ctypes.c_void_p()
    if (cuda.cuInit(0) or cuda.cuDeviceGet(ctypes.byref(dev), 0)
            or cuda.cuDevicePrimaryCtxRetain(ctypes.byref(ctx), dev) or cuda.cuCtxSetCurrent(ctx)):
        sys.exit(f"[gpu_hold] GPU {card}: CUDA init failed")
    return cuda


def card_free_mb(cuda: ctypes.CDLL, card: int) -> int:
    free, total = ctypes.c_size_t(), ctypes.c_size_t()
    if cuda.cuMemGetInfo_v2(ctypes.byref(free), ctypes.byref(total)):
        sys.exit(f"[gpu_hold] GPU {card}: cuMemGetInfo failed")
    return free.value >> 20


def ballast(card: int, parent: int) -> None:
    """Hold the one visible card's free VRAM but ``RESERVE_MB`` until killed."""
    ctypes.CDLL(None).prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    if os.getppid() != parent:
        return
    cuda = cuda_on_card(card)
    ptr = ctypes.c_uint64()
    chunks = (card_free_mb(cuda, card) - RESERVE_MB) // CHUNK_MB
    n = 0
    while n < chunks and cuda.cuMemAlloc_v2(ctypes.byref(ptr), ctypes.c_size_t(CHUNK_MB << 20)) == 0:
        n += 1
    log(card, f"holding {n * CHUNK_MB / 1024:g} GiB")
    while True:
        signal.pause()


def fill(card: int, parent: int, headroom_mb: int) -> None:
    """Keep the one visible card's free VRAM within ``[headroom_mb, headroom_mb + FILL_BAND_MB]``
    beside the job until killed, giving chunks back as soon as the job allocates."""
    ctypes.CDLL(None).prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    if os.getppid() != parent:
        return
    cuda = cuda_on_card(card)
    held, started, next_low_log = [], False, 0.0
    while True:
        free = card_free_mb(cuda, card)
        n = fill_step(free, len(held), headroom_mb)
        for _ in range(n):
            ptr = ctypes.c_uint64()
            if cuda.cuMemAlloc_v2(ctypes.byref(ptr), ctypes.c_size_t(CHUNK_MB << 20)):
                break
            held.append(ptr)
        for _ in range(-n):
            cuda.cuMemFree_v2(held.pop())
        if not started:
            started = True
            log(card, f"filling beside the job: holding {len(held) * CHUNK_MB / 1024:g} GiB, "
                      f"keeping {headroom_mb}..{headroom_mb + FILL_BAND_MB} MiB free")
        if free < headroom_mb // 2 and (now := time.monotonic()) >= next_low_log:
            next_low_log = now + 60
            log(card, f"free fell to {free} MiB")
        time.sleep(FILL_TICK_S)


def use(card: int, cmd: list[str]) -> None:
    os.makedirs(HOLD_DIR, exist_ok=True)
    fd = os.open(lock_path(card), os.O_CREAT | os.O_RDWR, 0o666)
    fcntl.flock(fd, fcntl.LOCK_SH)
    os.set_inheritable(fd, True)
    for sig in (signal.SIGPIPE, signal.SIGXFSZ):   # Python ignores these; exec would pass that on
        signal.signal(sig, signal.SIG_DFL)
    os.execvp(cmd[0], cmd)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("use", help="run a command on card <card> once the ballast has let go of it")
    p.add_argument("card", type=int)
    p.add_argument("cmd", nargs=argparse.REMAINDER, help="-- then the command")
    p = sub.add_parser("hold", help="started by gpu_wait.py --hold, one per claimed card")
    p.add_argument("card", type=int)
    p.add_argument("claim_pid", type=int)
    p.add_argument("smi_id", help="the card's UUID (or nvidia-smi index) for nvidia-smi -i")
    p.add_argument("--fill-mb", type=int, default=None,
                   help="once the job has settled on the card, fill its free VRAM down to this many MiB (off by default)")
    p = sub.add_parser("ballast", help="started by hold")
    p.add_argument("card", type=int)
    p.add_argument("parent", type=int)
    p = sub.add_parser("fill", help="started by hold --fill-mb")
    p.add_argument("card", type=int)
    p.add_argument("parent", type=int)
    p.add_argument("headroom_mb", type=int)
    args = ap.parse_args()
    if args.mode == "use":
        cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
        if not cmd:
            ap.error("no command given after --")
        use(args.card, cmd)
    elif args.mode == "hold":
        hold(args.card, args.claim_pid, args.smi_id, args.fill_mb)
    elif args.mode == "fill":
        fill(args.card, args.parent, args.headroom_mb)
    else:
        ballast(args.card, args.parent)


if __name__ == "__main__":
    main()

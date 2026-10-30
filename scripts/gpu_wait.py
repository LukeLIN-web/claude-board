"""Launch a GPU job that WAITS for a free card instead of stalling or failing.

On shared GPU boxes every card is often fully allocated. Rather than ask a
human which card to use (or crash on OOM), wrap the job: poll every N minutes
until enough cards have enough free VRAM, claim them, pin the job to them via
``CUDA_VISIBLE_DEVICES``, then exec the job in gpu_wait's place. With a max-wait
backstop so it never hangs forever.

Cards are named by **CUDA ordinal** everywhere — the pin, the claim lock,
``--exclude`` and ``release``. On boxes with a CUDA-invisible card the CUDA
ordinal differs from the nvidia-smi index, and using the smi index would send
the job to a different card than the one measured; see ``query_free_mb``.

This is the time-based "recheck every 20 min" behaviour a settings.json hook can
NOT express (hooks fire on events, not on a timer) — a poller is the right tool.

Usage::

  python scripts/gpu_wait.py --need-gb 40 --num-gpus 2 --interval-min 2 --timeout-min 1440 \\
      -- bash run_eval.sh
  python scripts/gpu_wait.py --need-gb 12 --slots 3 -- python sim.py   # up to 3 such jobs share a card
  python scripts/gpu_wait.py release <job pid> 3 4   # once the job has stopped its processes on CUDA 3 and 4

The wrapped command (everything after ``--``) runs with ``CUDA_VISIBLE_DEVICES``
set to the chosen cards. ``--exclude 3,6`` keeps named cards off-limits; a card
with uncorrectable ECC errors is off-limits the same way (see ``ecc_bad_gpus``).
``--hold`` keeps the claimed cards from looking idle to people who do not use
gpu_wait, for a job that starts every GPU process through ``gpu_hold.py use``
(see gpu_hold.py). Run under a launch of ``gpu_busy.py register`` (it inherited
the registration's lock fd), it records there the cards it claimed, and the launch
stops queuing (see gpu_busy.py). Call gpu_wait, gpu_busy and gpu_hold from one
copy: each reads what the others write.

A claim is an exclusive flock on a claim lock in ``LOCK_DIR`` (box-local: flock
on NFS is a lie), taken and held by the claim's holder, a ``gpu_wait.py claim``
process in a session of its own that carries the job's command, environment and
cwd, so whoever looks the lock's holder up in /proc/locks sees whose job it is.
The holder writes ``pid=<its pid> [slots=<K>] <the job's command>`` into the lock
(``note``). The holders of a job read one pipe whose write end the job inherits
on fd 10: a holder lets go once no process holds that end, so a job whose top
process was killed while children that inherited fd 10 still run (children
started with Python subprocess's default close_fds=True do not) keeps its cards,
and a job that is gone leaves no claim behind. A job that closes fd 10 (closes every fd above 2,
or redirects onto 10 by number) lets go of its cards at once, while it still
runs on them. Whatever gpu_wait itself inherited (a gpu_busy
registration among them) the holders keep while the job runs; the job gets the
pipe and nothing else. Foreign (non-gpu_wait) processes bypass the locks
entirely; the post-claim free-VRAM recheck narrows that race but nothing can
eliminate a non-cooperative racer.

A card has one claim lock per slot: ``gpu<k>.lock`` (slot 0, the whole-card
lock) and ``gpu<k>.s<s>.lock`` for slot s > 0. A job alone on its cards (the
default, ``--slots 1``) takes slot 0, and only while no other slot of the card is
held. With ``--slots K`` up to K jobs share a card, each holding one slot: slot 0
when it is free, recording ``slots=K`` in it, else the first free slot below K
when slot 0's holder recorded more than one slot.

``release <job pid> <cards>`` ends the job's claim holders on those cards and
nothing else: the job runs on with its other cards, and must not start anything
on a released card again (its ``CUDA_VISIBLE_DEVICES`` still names it). The job
is named by any of its pids (gpu_wait's own: it became the job); a card is the
job's when a holder of one of its claim locks reads the pipe the job holds. It
refuses, releasing none, when a card is not claimed for that job: unclaimed, the
job of another wrapper, or held by anything but a claim holder (a gpu_wait copy
that hands the lock fd itself to its job, another tool; such a claim ends only
with its job).

``LOCK_DIR``, the lock names and the note are the claim protocol every copy of
gpu_wait on a box shares, older copies in other repos among them: changing them
lets two pollers load one card.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import re
import signal
import subprocess
import sys
import time

LOCK_DIR = "/tmp/gpu_wait_locks"
LOCK_NAME = re.compile(r"gpu(\d+)(?:\.s(\d+))?\.lock$")   # card, then slot (none: slot 0)
SELF = os.path.abspath(__file__)
HERE = os.path.dirname(SELF)
HOLD = os.path.join(HERE, "gpu_hold.py")
BUSY = os.path.join(HERE, "gpu_busy.py")

STALL_ALERT_S = 3600.0
TIMEOUT_MIN = 1440.0   # --timeout-min's default; also how long gpu_busy keeps a launch's place for its name
LOCKS_READS = 10   # /proc/locks comes a page per read: a held lock's line can slip between pages while other locks come and go
RELEASE_WAIT_S = 10.0
HEIR_FD = 10
END = signal.SIGUSR1   # the one signal a claim holder dies of
NOTE_CHARS = 200


def _wait_prefix(waited_s: float, stall_alert_s: float) -> str:
    """``[gpu_wait]``, escalating to a greppable ``STALLED`` marker once the wait
    exceeds ``stall_alert_s`` — a monitor tailing the log alerts on that token
    instead of a silent multi-hour sleep (a wait loop once sat 7 h behind a
    foreign job with no signal)."""
    if waited_s >= stall_alert_s:
        return f"[gpu_wait] STALLED {waited_s / 60:.0f}min:"
    return "[gpu_wait]"


def _pairs(text: str):
    """``key, int`` CSV rows; blank lines ignored."""
    for line in text.splitlines():
        line = line.strip()
        if line:
            key, value = line.split(",")
            yield key.strip(), int(value.strip())


def parse_free_mb(text: str) -> dict[int, int]:
    """Parse ``index, free MiB`` rows (as ``nvidia-smi --query-gpu=index,memory.free
    --format=csv,noheader,nounits`` prints them) into ``{index: free_mb}``; blank
    lines are ignored."""
    return {int(k): v for k, v in _pairs(text)}


ECC_QUERY = ("index,uuid,ecc.errors.uncorrected.volatile.total,"
             "ecc.errors.uncorrected.aggregate.total")


def parse_ecc(text: str) -> list[tuple[int, str, int, int]]:
    """Parse ``nvidia-smi --query-gpu=<ECC_QUERY> --format=csv,noheader,nounits``
    into ``[(smi_index, uuid, volatile, aggregate)]``. A non-numeric counter
    (``[N/A]`` / ``[Not Supported]``: ECC off or unsupported) counts as 0."""
    rows = []
    for line in text.splitlines():
        if line.strip():
            index, uuid, *counts = (f.strip() for f in line.split(","))
            volatile, aggregate = (int(c) if c.isdigit() else 0 for c in counts)
            rows.append((int(index), uuid, volatile, aggregate))
    return rows


_REPORTED: set[str] = set()


def _say_once(key: str, msg: str) -> None:
    """``[gpu_wait] msg`` on stderr the first time ``key`` comes up this run, not
    on every poll."""
    if key not in _REPORTED:
        _REPORTED.add(key)
        print(f"[gpu_wait] {msg}", file=sys.stderr, flush=True)


# ── the claim protocol ───────────────────────────────────────────────────────────


def claim_lock(card: int, slot: int = 0) -> str:
    return os.path.join(LOCK_DIR, f"gpu{card}.lock" if slot == 0 else f"gpu{card}.s{slot}.lock")


def claim_locks() -> list[tuple[int, int, str]]:
    """``(card, slot, path)`` of every claim lock in ``LOCK_DIR``, by card, then slot."""
    try:
        names = os.listdir(LOCK_DIR)
    except OSError:
        return []
    return sorted((int(m[1]), int(m[2] or 0), os.path.join(LOCK_DIR, name))
                  for name in names if (m := LOCK_NAME.match(name)))


def card_locks(card: int) -> list[str]:
    """Card's claim locks: slot 0, then every other slot one has been made for."""
    return [claim_lock(card)] + [path for c, slot, path in claim_locks() if c == card and slot]


def note(pid: int, slots: int, job: list[str]) -> str:
    """What the holder ``pid`` of a claim writes into its lock: ``slots=`` only on a card shared by more than one job."""
    return f"pid={pid}{f' slots={slots}' if slots > 1 else ''} {' '.join(job)[:NOTE_CHARS]}\n"


def read_note(path: str) -> tuple[int | None, int, str]:
    """``(pid, slots, job command)`` from the note in a claim lock; ``(None, 1, "")``
    without one. The pid names whoever wrote it, who may be long gone: the holder
    is the pid /proc/locks lists."""
    try:
        with open(path, errors="replace") as f:
            words = f.readline().split()
    except OSError:
        return None, 1, ""
    pid = int(words[0][4:]) if words and words[0].startswith("pid=") and words[0][4:].isdigit() else None
    slots = 1
    if words[1:2] and words[1].startswith("slots=") and words[1][6:].isdigit():
        slots = int(words.pop(1)[6:])
    return pid, slots, " ".join(words[1:]) if pid is not None else ""


def shared(card: int) -> bool:
    """The holder of card's slot 0 recorded more than one slot: other jobs may take the card's other slots."""
    return read_note(claim_lock(card))[1] > 1


def file_id(path: str) -> str | None:
    """``path``'s file as /proc/locks names it, ``maj:min:inode``; ``None`` when it does not exist."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"


def flock_table() -> list[tuple[bool, int, str]]:
    """``(waiting, pid, file)`` for every flock /proc/locks lists (a waiter's line
    carries ``->``), ``file`` as ``file_id`` gives it."""
    out = []
    with open("/proc/locks") as f:
        for line in f:
            parts = line.split()[1:]
            waiting = parts[:1] == ["->"]
            parts = parts[waiting:]
            if len(parts) > 4 and parts[0] == "FLOCK":
                out.append((waiting, int(parts[3]), parts[4]))
    return out


def flocks(path: str) -> list[tuple[bool, int]]:
    """``[(waiting, pid)]`` for every flock held or waited for on ``path`` (none when it does not exist)."""
    fid = file_id(path)
    return [] if fid is None else [(waiting, pid) for waiting, pid, f in flock_table() if f == fid]


def held_claims() -> list[tuple[int, str, int]]:
    """``(card, claim lock, holder pid)`` for every claim lock /proc/locks lists as held, from
    one read of it (a line that slips between its pages leaves that claim out)."""
    holder = {f: pid for waiting, pid, f in flock_table() if not waiting}
    return [(card, path, holder[f]) for card, _, path in claim_locks() if (f := file_id(path)) in holder]


def held(path: str) -> bool:
    """Somebody holds an flock on ``path``, this process through an inherited fd included."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def lock_holder(path: str) -> int | None:
    """The pid /proc/locks lists as holder of the flock on ``path``; ``None`` when
    none of ``LOCKS_READS`` reads lists one."""
    for _ in range(LOCKS_READS):
        if pids := [pid for waiting, pid in flocks(path) if not waiting]:
            return pids[0]
    return None


def argv_of(pid: int) -> list[str]:
    """The process's argv; empty once it is gone or exiting."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().decode(errors="replace").split("\0")[:-1]
    except OSError:
        return []


def exited(pid: int) -> bool:
    """The process is gone or a zombie: it has let go of its files and their locks
    (an exiting process drops its argv before them)."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rpartition(")")[2].split()[0] in ("Z", "X")
    except OSError:
        return True


def is_holder(argv: list[str], path: str) -> bool:
    """``argv`` is that of the holder of claim lock ``path``: ``python …/gpu_wait.py claim <path> <slots> -- <job>``."""
    return argv[2:4] == ["claim", path] and argv[5:6] == ["--"] and os.path.basename(argv[1]) == "gpu_wait.py"


def holder_job(argv: list[str]) -> str:
    """The job's command in a claim holder's argv."""
    return " ".join(argv[6:])[:160]


def claim_holder(path: str) -> int | None:
    """The pid of the claim holder holding claim lock ``path``; ``None`` when the
    lock is free or anything else holds it."""
    pid = lock_holder(path)
    return pid if pid is not None and is_holder(argv_of(pid), path) else None


def key(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_dev, st.st_ino


def open_files(pid: int | str = "self") -> dict[tuple[int, int], int]:
    """Every file process ``pid`` has open, with an fd on it; empty once it is gone.
    A lock fd it inherited makes that lock its own."""
    files = {}
    try:
        fds = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return files
    for fd in fds:
        if (k := key(f"/proc/{pid}/fd/{fd}")) is not None:
            files[k] = int(fd)
    return files


def reads_pipe_of(holder: int, files: dict[tuple[int, int], int]) -> bool:
    """The claim holder ``holder`` reads a pipe whose write end is among ``files``."""
    return key(f"/proc/{holder}/fd/0") in files


# ── claiming ─────────────────────────────────────────────────────────────────────


def detach(argv: list[str], **kw) -> subprocess.CompletedProcess:
    """Start ``argv`` in a session of its own, orphaned at once: no child of this
    process, which becomes the job, so the job never sees or reaps it."""
    return subprocess.run(["setsid", "-f", *argv], check=True, **kw)


def end(holder: int) -> None:
    try:
        os.kill(holder, END)
    except ProcessLookupError:
        pass


def hold_claim(path: str, slots: int, job: list[str]) -> None:
    """``claim <claim lock> <slots> -- <job>``: take the lock for ``job`` and print
    this process's pid, then keep the lock until stdin reads EOF; exit at once if
    somebody else holds it."""
    signal.signal(END, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {END})
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, signal.SIG_IGN)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return
    os.ftruncate(fd, 0)
    os.write(fd, note(os.getpid(), slots, job).encode())
    print(os.getpid(), flush=True)
    os.dup2(2, 1)
    while os.read(0, 4096):
        pass


def try_claim(card: int, cmd: list[str], life: int, slot: int = 0, slots: int = 1) -> int | None:
    """Claim slot ``slot`` of card ``card`` for ``cmd``: the pid of its claim holder,
    which holds the claim lock until ``life`` (a pipe's read end) reads EOF;
    ``None`` if somebody else holds the lock."""
    out = detach([sys.executable, SELF, "claim", claim_lock(card, slot), str(slots), "--", *cmd],
                 stdin=life, stdout=subprocess.PIPE, close_fds=False, text=True).stdout
    return int(out) if out else None


def claim_card(card: int, slots: int, cmd: list[str], life: int) -> int | None:
    """Claim a slot of ``card`` for ``cmd`` (see the module docstring): the pid of its
    claim holder, or ``None`` when the card has no slot this job may take."""
    for slot in range(slots):
        if (holder := try_claim(card, cmd, life, slot, slots)) is not None:
            if slots == 1 and any(held(path) for path in card_locks(card)[1:]):
                end(holder)   # other jobs share the card
                return None
            if slot > 0 and not shared(card):
                end(holder)   # slot 0 changed hands to a job alone on the card
                return None
            return holder
        if slot == 0 and not shared(card):
            return None
    return None


def release(job: int, cards: list[int]) -> None:
    """End ``job``'s claim holders on ``cards``; none when any card is not claimed for
    ``job`` (a pid of the job: it holds the pipe the holders read)."""
    holders, refused = [], []
    files = open_files(job)
    for card in cards:
        claims = [(path, pid, argv_of(pid)) for path in card_locks(card) if (pid := lock_holder(path)) is not None]
        mine = [(pid, argv) for path, pid, argv in claims if is_holder(argv, path) and reads_pipe_of(pid, files)]
        if mine:
            pid, argv = mine[0]
            holders.append((card, pid, holder_job(argv)))
        elif not claims:
            refused.append(f"GPU {card} is not claimed")
        elif not is_holder(claims[0][2], claims[0][0]):
            _, pid, argv = claims[0]
            refused.append(f"GPU {card}: its claim lock is held by pid {pid} ({' '.join(argv)[:160] or 'exited'}), not by "
                           "a gpu_wait claim holder: a gpu_wait copy that hands the lock fd to its job, or another tool; "
                           "only the end of that job frees the card")
        else:
            refused.append(f"GPU {card} is claimed for another job ({holder_job(claims[0][2])}), not for pid {job}'s")
    if refused:
        sys.exit("\n".join(f"[gpu_wait] not releasing: {why}" for why in refused))
    for _, pid, _ in holders:
        end(pid)
    deadline = time.monotonic() + RELEASE_WAIT_S
    for card, pid, cmd in holders:
        while not exited(pid):
            if time.monotonic() > deadline:
                sys.exit(f"[gpu_wait] the claim holder of GPU {card} (pid {pid}) is still there after SIGUSR1")
            time.sleep(0.01)
        print(f"[gpu_wait] released GPU {card} (claimed for: {cmd})", flush=True)


def claim_free_gpus(need_gb: float, num_gpus: int, exclude: tuple[int, ...],
                    cmd: list[str], life: int, slots: int = 1) -> list[tuple[int, int]] | None:
    """Pick AND claim ``num_gpus`` cards for ``cmd`` in one step — ``[(idx, holder pid)]``.

    All-or-nothing: candidates are tried most-free-first; a card with no slot this
    job may take is skipped, and a card that lost its free VRAM between the scan
    and the claim (a non-cooperative loader won the race) is released and skipped.
    Fewer than ``num_gpus`` claimable -> every claim taken is released and ``None``
    returned."""
    need_mb = need_gb * 1024
    free = query_free_mb()
    skip = set(exclude) | ecc_bad_gpus()
    order = sorted(((i, mb) for i, mb in free.items()
                    if i not in skip and mb >= need_mb),
                   key=lambda im: (-im[1], im[0]))
    taken: list[tuple[int, int]] = []
    for idx, _ in order:
        holder = claim_card(idx, slots, cmd, life)
        if holder is None:
            continue
        if query_free_mb().get(idx, 0) < need_mb:
            end(holder)
            continue
        taken.append((idx, holder))
        if len(taken) == num_gpus:
            return taken
    for _, holder in taken:
        end(holder)
    return None


def wait_and_claim_gpus(need_gb: float, *, num_gpus: int = 1, interval_s: float,
                        timeout_s: float, exclude: tuple[int, ...] = (),
                        log=print, stall_alert_s: float = STALL_ALERT_S,
                        cmd: list[str], life: int, slots: int = 1) -> list[tuple[int, int]] | None:
    """Poll until ``num_gpus`` cards are both free AND claimable and claim them for
    ``cmd`` (``claim_free_gpus``), or ``None`` on timeout."""
    start = time.monotonic()
    deadline = start + timeout_s
    while True:
        taken = claim_free_gpus(need_gb, num_gpus, exclude, cmd, life, slots)
        if taken is not None:
            return taken
        now = time.monotonic()
        if now >= deadline:
            return None
        log(f"{_wait_prefix(now - start, stall_alert_s)} no {num_gpus} "
            f"claimable card(s) with {need_gb:g} GB free; re-checking in "
            f"{interval_s / 60:g} min", flush=True)
        time.sleep(interval_s)


# ── which card is which ──────────────────────────────────────────────────────────


def _probe(code: str, cvd: str | None = None) -> str | None:
    """Run a snippet in a throwaway interpreter with CUDA_VISIBLE_DEVICES stripped
    (or set to ``cvd``), so it sees the machine's real CUDA enumeration and leaves
    no CUDA state behind."""
    env = {k: v for k, v in os.environ.items() if k != "CUDA_VISIBLE_DEVICES"}
    if cvd is not None:
        env["CUDA_VISIBLE_DEVICES"] = cvd
    try:
        return subprocess.check_output([sys.executable, "-c", code], text=True,
                                       env=env, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001 — no CUDA driver / no torch
        return None


def _smi(query: str) -> str | None:
    try:
        return subprocess.check_output(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            text=True)
    except Exception:  # noqa: BLE001 — no nvidia-smi / NVML mismatch
        return None


UUID_PROBE = ("import ctypes\n"
              "cu = ctypes.CDLL('libcuda.so.1')\n"
              "n = ctypes.c_int()\n"
              "assert cu.cuInit(0) == 0 and cu.cuDeviceGetCount(ctypes.byref(n)) == 0\n"
              "for i in range(n.value):\n"
              "    d, u = ctypes.c_int(), (ctypes.c_ubyte * 16)()\n"
              "    if cu.cuDeviceGet(ctypes.byref(d), i) == 0 and cu.cuDeviceGetUuid(u, d) == 0:\n"
              "        h = bytes(u).hex()\n"
              "        print(f'{i}, GPU-{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}')\n")


def _uuids(out: str | None) -> dict[int, str]:
    return {int(ordinal): uuid.strip() for ordinal, uuid in
            (line.split(",", 1) for line in (out or "").splitlines() if line.strip())}


_CUDA_UUIDS: dict[int, str] = {}


def cuda_uuids() -> dict[int, str]:
    """``{cuda_ordinal: GPU-uuid}`` — the CUDA enumeration, asked of the CUDA driver
    (libcuda) and cached per process once it finds a card.

    This is the ONLY authority on which physical card a ``CUDA_VISIBLE_DEVICES``
    value selects, and the two orderings genuinely disagree: a card CUDA cannot
    see (some boxes have one) is absent here but still occupies an nvidia-smi
    index, shifting every higher smi index by one. The driver is asked rather than
    torch: on such a box torch's ``device_count()`` over-reports and its first CUDA
    call asserts. Reading UUIDs creates no context. When the whole enumeration
    fails (one bad card can fail it), each ordinal is asked in a process of its
    own; an empty answer is not cached, so the next poll asks again.
    """
    global _CUDA_UUIDS
    if not _CUDA_UUIDS:
        found = _uuids(_probe(UUID_PROBE))
        if not found:
            for i in range(len((_smi("index") or "").split())):
                if one := _uuids(_probe(UUID_PROBE, cvd=str(i))):
                    found[i] = one[0]
        _CUDA_UUIDS = found
    return _CUDA_UUIDS


def query_free_mb() -> dict[int, int]:
    """Live ``{cuda_ordinal: free_mb}`` — keys are CUDA ordinals, NOT smi indices.

    That distinction is the whole point: these keys are handed straight to
    ``CUDA_VISIBLE_DEVICES`` and used to name the per-card claim lock, so keying
    on smi indices would both land the job on a different card than the one it
    measured and record the claim against a card nobody is using. Free VRAM is
    read passively from nvidia-smi and joined onto the CUDA enumeration by UUID;
    a card with no CUDA counterpart drops out of the pool, which is what makes
    the "poison card reports 95G free" mirage unpickable.

    No usable nvidia-smi (NVML driver/library mismatch — smi refuses to init
    while CUDA still works) falls back to torch's own ``mem_get_info``, still
    CUDA-keyed. Empty dict (no card to claim this poll) when the CUDA enumeration
    finds none or every probe fails: smi indices are never used.
    """
    uuid_of = cuda_uuids()
    if not uuid_of:
        _say_once("enumeration", "the CUDA driver enumerates no card; asking again every poll")
        return {}
    smi = _smi("uuid,memory.free")
    if smi is not None:
        free = dict(_pairs(smi))
        return {i: free[u] for i, u in uuid_of.items() if u in free}
    out = _probe("import torch\n"
                 "for i in range(torch.cuda.device_count()):\n"
                 "    try: print(f'{i}, {torch.cuda.mem_get_info(i)[0] >> 20}')\n"
                 "    except Exception: pass\n")
    return {} if out is None else parse_free_mb(out)


def ecc_bad_gpus() -> set[int]:
    """Cards with uncorrectable ECC errors (volatile OR aggregate count > 0),
    keyed like ``query_free_mb``: CUDA ordinals joined by UUID; a CUDA-invisible
    card is already out of the pool.

    Such a card reads 0 MiB used, so most-free-first picks it first and the job
    dies (``cudaErrorECCUncorrectable``, NCCL init) or hangs. A failed query
    (old driver, field unsupported, no smi) screens nothing and never fails the
    claim.
    """
    smi = _smi(ECC_QUERY)
    try:
        rows = None if smi is None else parse_ecc(smi)
    except ValueError:  # an output shape we do not know: same as no answer
        rows = None
    if rows is None:
        _say_once("query", "uncorrected-ECC query failed; cards are NOT screened "
                  "for ECC errors")
        return set()
    ordinal_of = {u: i for i, u in cuda_uuids().items()}
    bad = set()
    for index, uuid, volatile, aggregate in rows:
        gpu = ordinal_of.get(uuid)
        if gpu is not None and (volatile or aggregate):
            bad.add(gpu)
            _say_once(uuid, f"skipping GPU {gpu} (nvidia-smi index {index}, {uuid}) "
                      f"for uncorrectable ECC errors: volatile {volatile}, "
                      f"aggregate {aggregate}")
    return bad


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["claim"]:
        hold_claim(argv[1], int(argv[2]), argv[4:])
        return
    if argv[:1] == ["release"]:
        ap = argparse.ArgumentParser(prog="gpu_wait.py release",
                                     description="release the job's claims on these cards, keeping its others; "
                                                 "stop the job's processes on them first")
        ap.add_argument("job", type=int, help="pid of a process of the job (gpu_wait's own pid: it became the job)")
        ap.add_argument("cards", type=int, nargs="+", help="CUDA ordinals")
        args = ap.parse_args(argv[1:])
        release(args.job, args.cards)
        return
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--need-gb", type=float, default=60.0,
                    help="free VRAM each claimed card must have (per card)")
    ap.add_argument("--num-gpus", type=int, default=1,
                    help="how many cards to claim (all-or-nothing)")
    ap.add_argument("--slots", type=int, default=1,
                    help="jobs that may share one card, each holding one of its slots (default: the card is the job's alone)")
    ap.add_argument("--interval-min", type=float, default=20.0)
    ap.add_argument("--timeout-min", type=float, default=TIMEOUT_MIN)
    ap.add_argument("--stall-alert-min", type=float, default=60.0,
                    help="after this long waiting, heartbeat lines carry a "
                         "greppable STALLED marker (monitor-alert hook)")
    ap.add_argument("--exclude", default="",
                    help="comma-separated CUDA ordinals to skip (NOT nvidia-smi "
                         "indices; they differ where a card is CUDA-invisible)")
    ap.add_argument("--hold", action="store_true",
                    help="while the claim lives, hold each claimed card's free VRAM "
                         "whenever it is idle (gpu_hold.py); the job must start its "
                         "GPU processes through `gpu_hold.py use`")
    ap.add_argument("--fill-headroom-mb", type=int, default=None,
                    help="with --hold: once the job has settled on a card, fill the "
                         "card's free VRAM down to this many MiB (gpu_hold.py)")
    ap.add_argument("cmd", nargs=argparse.REMAINDER,
                    help="-- then the command to run on the freed card(s)")
    args = ap.parse_args(argv)

    cmd = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else args.cmd
    if not cmd:
        ap.error("no command given after --")
    if args.slots < 1:
        ap.error("--slots must be at least 1")
    if args.hold and args.slots > 1:
        ap.error("--hold keeps each card for one job: not with --slots")
    if args.fill_headroom_mb is not None and not args.hold:
        ap.error("--fill-headroom-mb needs --hold")
    exclude = tuple(int(x) for x in args.exclude.split(",") if x.strip())
    os.makedirs(LOCK_DIR, exist_ok=True)

    life, heir = os.pipe()   # the claim holders read `life`; the job inherits `heir`
    taken = wait_and_claim_gpus(args.need_gb, num_gpus=args.num_gpus,
                                interval_s=args.interval_min * 60,
                                timeout_s=args.timeout_min * 60, exclude=exclude,
                                stall_alert_s=args.stall_alert_min * 60,
                                cmd=cmd, life=life, slots=args.slots)
    if taken is None:
        print(f"[gpu_wait] timed out after {args.timeout_min:g} min — no "
              f"{args.num_gpus} free card(s)", file=sys.stderr, flush=True)
        sys.exit(2)
    pin = ",".join(str(g) for g, _ in taken)
    print(f"[gpu_wait] claimed + running on GPU {pin}", flush=True)
    # gpu_busy finds the launch by the registration fd this process inherited
    subprocess.run([sys.executable, BUSY, "claimed", pin], close_fds=False)
    if args.hold:   # not handed `heir`: a hold must not keep the claim alive
        fill = [] if args.fill_headroom_mb is None else ["--fill-mb", str(args.fill_headroom_mb)]
        for g, holder in taken:
            detach([sys.executable, HOLD, "hold", str(g), str(holder), cuda_uuids().get(g, str(g)), *fill])
    os.dup2(heir, HEIR_FD)   # above the 3-9 a job script redirects by number (exec 3>&1 4>&2)
    os.set_inheritable(HEIR_FD, True)   # dup2 onto itself leaves the flag
    os.closerange(3, HEIR_FD)
    os.closerange(HEIR_FD + 1, os.sysconf("SC_OPEN_MAX"))
    for sig in (signal.SIGPIPE, signal.SIGXFSZ):   # Python ignores these; exec would pass that on
        signal.signal(sig, signal.SIG_DFL)
    os.execvpe(cmd[0], cmd, dict(os.environ, CUDA_VISIBLE_DEVICES=pin))


if __name__ == "__main__":
    main()

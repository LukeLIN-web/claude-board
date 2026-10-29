"""This host's GPUs — each card, the processes on it, and the board card whose
session started each one — so one page shows who holds which card.

WHY IT RIDES THE SNAPSHOT: who holds a card is nvidia-smi joined with the
sessions the board already lists, and every host's half has to reach one page.
The peer poll already carries each host's cards to the aggregating board, so
this host's sample goes out in the same local snapshot (`gpus`) and the
aggregator merges the lists the way it merges the cards. One GET /api/windows
then answers "who holds what" for every machine.

WHO STARTED IT: a process launched from a Claude Code Bash tool inherits
CLAUDE_CODE_SESSION_ID, and `attach` names the card with that session_id. A job
started over ssh from another machine carries no session id; what is left to
go on is where its ssh came from (`ssh_from`), its cwd and its command.

GPU_WAIT CLAIMS: where jobs queue for cards through scripts/gpu_wait.py, each
sample also lists the held claims and the gpu_wait.py processes still waiting
for one. gpu_wait claims a card by holding a flock on one of its claim locks in
its LOCK_DIR, with "pid=<pid> …" and the command written inside; the lock
directory, the lock names and that note are gpu_wait's, read here through it.
The holder is the pid the kernel lists for the flock, not the pid written in
the file: another tool can flock the same file without rewriting it, and the
note left inside then names an earlier holder long gone.
A CUDA ordinal is not the nvidia-smi index on a box with a card CUDA can't see,
so claims are listed beside the cards rather than placed on one.

NEVER BLOCKS: nvidia-smi can hang for seconds on a card in trouble, so a thread
samples every SAMPLE_INTERVAL and the snapshot reads the last sample — the same
rule as the peer poll. A host with no working nvidia-smi has no entry at all.
"""
from __future__ import annotations

import copy
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from .sessions import proc_environ

sys.path.append(str(Path(__file__).resolve().parents[1] / "scripts"))
import gpu_wait  # noqa: E402

SAMPLE_INTERVAL = 15.0
SMI_TIMEOUT = 10.0
# A sample this old means the sampler is stuck (nvidia-smi hung), and the page
# dims the host's GPUs rather than showing them as current.
STALE_AFTER = 3 * SAMPLE_INTERVAL
CMD_CHARS = 300

_CLK = os.sysconf("SC_CLK_TCK")

# The last good sample; replaced whole, never edited, so readers need no lock.
_latest: Optional[dict] = None
_thread: Optional[threading.Thread] = None


def _smi(kind: str, fields: str) -> list[list[str]]:
    out = subprocess.run(["nvidia-smi", f"--query-{kind}={fields}", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, check=True, timeout=SMI_TIMEOUT).stdout
    return [[c.strip() for c in line.split(",")] for line in out.splitlines() if line.strip()]


def _num(x: str) -> Optional[int]:
    return int(x) if x.isdigit() else None  # "[N/A]" on a card CUDA can't see


def _read(pid: int, name: str) -> bytes:
    try:
        with open(f"/proc/{pid}/{name}", "rb") as f:
            return f.read()
    except OSError:
        return b""


def _cmdline(pid: int) -> str:
    return _read(pid, "cmdline").replace(b"\0", b" ").decode(errors="replace").strip()


def _boot_time() -> float:
    with open("/proc/stat") as f:
        return float(next(line.split()[1] for line in f if line.startswith("btime")))


def _proc(pid: int, boot: float) -> dict:
    """What a process runs and who started it. Times are epoch seconds rather
    than ages, so an unchanged process reads the same in every sample."""
    env = proc_environ(pid)
    stat = _read(pid, "stat").decode(errors="replace")
    rest = stat[stat.rfind(")") + 2:].split() if stat else []
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        cwd = ""
    cmd = _cmdline(pid)
    if rest and "/" not in cmd:  # a renamed worker (VLLM::EngineCore): its parent says what it serves
        cmd += " <- " + _cmdline(int(rest[1]))
    return {
        "pid": pid,
        "started_at": round(boot + int(rest[19]) / _CLK) if rest else None,
        "cwd": cwd,
        "cmd": cmd[:CMD_CHARS],
        "session": env.get("CLAUDE_CODE_SESSION_ID"),
        "ssh_from": env.get("SSH_CONNECTION", "").split(" ")[0] or None,
    }


def _claims(boot: float) -> list[dict]:
    """Held gpu_wait claims, one per holding process, with the CUDA ordinals it
    holds. The kernel drops a flock when its last holder dies, so a lock file
    nobody holds is a finished claim, not a live one. The note inside names the
    job only when its holder wrote it."""
    by_pid: dict[int, dict] = {}
    for card, path, pid in gpu_wait.held_claims():
        claim = by_pid.setdefault(pid, dict(_proc(pid, boot), cuda=[]))
        written_by, _, job = gpu_wait.read_note(path)
        if written_by == pid:
            claim["cmd"] = job[:CMD_CHARS]
        claim["cuda"] = sorted(set(claim["cuda"]) | {card})
    return list(by_pid.values())


def _waiting(claimed: set[int], boot: float) -> list[dict]:
    """gpu_wait.py processes that hold no claim: still waiting for cards. Once
    it has claimed, gpu_wait.py execs into its job, whose argv is the job's, and
    its claim holders (``gpu_wait.py claim …``) hold the claims; an older copy
    that stays on as its job's parent holds the claim locks itself. Either way
    no pid of a claim is waiting."""
    out = []
    for d in os.listdir("/proc"):
        if d.isdigit() and int(d) not in claimed:
            argv = _read(int(d), "cmdline").split(b"\0")[:3]
            if any(a.endswith(b"gpu_wait.py") for a in argv):
                out.append(_proc(int(d), boot))
    return sorted(out, key=lambda p: p["pid"])


def sample() -> Optional[dict]:
    """One reading of this host's GPUs, or None when nvidia-smi can't give one."""
    try:
        rows = _smi("gpu", "index,uuid,memory.used,memory.total,utilization.gpu")
        apps = _smi("compute-apps", "gpu_uuid,pid,used_memory")
    except (OSError, subprocess.SubprocessError):
        return None
    boot = _boot_time()
    cards = [{"index": int(i), "used_mb": _num(used), "total_mb": _num(total), "util": _num(util), "procs": []}
             for i, _uuid, used, total, util in rows]
    by_uuid = {row[1]: card for row, card in zip(rows, cards)}
    for uuid, pid, mb in apps:
        # A process in another container's pid namespace shows no pid here.
        p = _proc(int(pid), boot) if pid.isdigit() else {"pid": None, "cmd": "", "session": None}
        by_uuid[uuid]["procs"].append(dict(p, used_mb=_num(mb)))
    queue = os.path.isdir(gpu_wait.LOCK_DIR)   # gpu_wait makes it before it waits
    claims = _claims(boot) if queue else []
    waiting = _waiting({c["pid"] for c in claims}, boot) if queue else []
    return {"ts": time.time(), "cards": cards, "claims": claims, "waiting": waiting}


def _sample_loop() -> None:
    global _latest
    while True:
        try:
            s = sample()
            if s is not None:
                _latest = s
        except Exception as e:
            print(f"[gpus] sample error: {e}")
        time.sleep(SAMPLE_INTERVAL)


def start() -> None:
    """Start the sampler thread. Idempotent."""
    global _thread
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_sample_loop, name="fleet-gpus", daemon=True)
    _thread.start()


def local(label: str) -> list[dict]:
    """This host's entry for the snapshot: [] without GPUs, else one host row.
    A copy, since `attach` writes card names into the rows it is given."""
    s = _latest
    if s is None:
        return []
    row = {k: v for k, v in s.items() if k != "ts"}  # ts ticks; `stale` says what it means
    return [dict(copy.deepcopy(row), host=label, stale=time.time() - s["ts"] > STALE_AFTER)]


def attach(snap: dict) -> None:
    """Name the board card behind every GPU process, claim and waiter, and list
    on each card the GPUs (host:index) its session's processes are on."""
    card_of = {w["session_id"]: w["key"] for w in snap["windows"] if w.get("session_id")}
    on: dict[str, dict[str, None]] = {}
    for h in snap["gpus"]:
        for c in h["cards"]:
            for p in c["procs"]:
                p["card"] = card_of.get(p["session"])
                if p["card"]:
                    on.setdefault(p["card"], {})[f'{h["host"]}:{c["index"]}'] = None
        for p in h["claims"] + h["waiting"]:
            p["card"] = card_of.get(p["session"])
    for w in snap["windows"]:
        w["gpus"] = list(on.get(w["key"], {}))

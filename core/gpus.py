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

GPU_WAIT CLAIMS: where jobs queue for cards through gpu_wait.py, set
FLEET_GPU_LOCK_DIR to its lock directory. gpu_wait claims a card by holding a
flock on gpu<N>.lock (further slots of a shared card: gpu<N>.s<K>.lock), N the
CUDA ordinal, with "pid=<pid> …" and the command written inside. Each sample
then lists the held claims and the gpu_wait.py processes still waiting for one.
A CUDA ordinal is not the nvidia-smi index on a box with a card CUDA can't see,
so claims are listed beside the cards rather than placed on one.

NEVER BLOCKS: nvidia-smi can hang for seconds on a card in trouble, so a thread
samples every SAMPLE_INTERVAL and the snapshot reads the last sample — the same
rule as the peer poll. A host with no working nvidia-smi has no entry at all.
"""
from __future__ import annotations

import copy
import os
import re
import subprocess
import threading
import time
from typing import Optional

SAMPLE_INTERVAL = 15.0
SMI_TIMEOUT = 10.0
# A sample this old means the sampler is stuck (nvidia-smi hung), and the page
# dims the host's GPUs rather than showing them as current.
STALE_AFTER = 3 * SAMPLE_INTERVAL
CMD_CHARS = 300

_CLAIM = re.compile(r"gpu(\d+)(?:\.s\d+)?\.lock$")
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
    env = dict(kv.split("=", 1) for kv in _read(pid, "environ").decode(errors="replace").split("\0") if "=" in kv)
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


def _claims(lock_dir: str, boot: float) -> list[dict]:
    """Held gpu_wait claims, one per claiming process, with the CUDA ordinals it
    holds. The kernel drops a flock when its last holder dies, so a lock file
    nobody holds is a finished claim, not a live one."""
    held = set()
    with open("/proc/locks") as f:
        for line in f:
            parts = line.split()
            if len(parts) > 5 and parts[1] == "FLOCK":
                held.add(parts[5])  # maj:min:inode of the locked file
    by_pid: dict[int, dict] = {}
    for name in sorted(os.listdir(lock_dir)):
        m = _CLAIM.match(name)
        path = os.path.join(lock_dir, name)
        st = os.stat(path)
        if not m or f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}" not in held:
            continue
        with open(path, errors="replace") as f:
            words = f.read(CMD_CHARS).split()
        if not words or not words[0].startswith("pid="):
            continue
        pid = int(words[0][4:])
        note = " ".join(w for w in words[1:] if not w.startswith("slots="))
        claim = by_pid.setdefault(pid, dict(_proc(pid, boot), cuda=[], cmd=note))
        claim["cuda"] = sorted(set(claim["cuda"]) | {int(m.group(1))})
    return list(by_pid.values())


def _waiting(claimed: set[int], boot: float) -> list[dict]:
    """gpu_wait.py processes holding no claim yet. After a claim the wrapper
    stays on as the job's parent, so its pid is in a claim and drops out."""
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
    lock_dir = os.environ.get("FLEET_GPU_LOCK_DIR", "").strip()
    claims = _claims(lock_dir, boot) if lock_dir and os.path.isdir(lock_dir) else []
    waiting = _waiting({c["pid"] for c in claims}, boot) if lock_dir else []
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

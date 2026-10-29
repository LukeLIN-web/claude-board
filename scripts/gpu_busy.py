"""Is this box free for my launch? The one busy check that runners and service
launch scripts call, instead of each writing its own.

``cards`` -- one line: the cards that can be claimed now (``-`` when none), then
every CUDA ordinal on the box, each comma-separated, so a waiter hands gpu_wait
``--exclude`` the rest. A card qualifies when CUDA sees it, it has no uncorrectable
ECC errors, it is not in ``--exclude``, no other gpu_wait holds a claim lock of it (any
slot), and nvidia-smi shows nobody on it: no compute process and at most ``IDLE_MB`` used
(graphics-only processes use memory without being listed). Everyone on these boxes
runs as one Linux account, so whose process it is cannot be told and does not
matter. On a card claimed for this process's job (it inherited the claim from
gpu_wait: the pipe the claim holder reads, or the lock fd itself from a copy that
hands it to its job), that claim's ballast (``BALLAST_NAME``, from gpu_wait
--hold) is not somebody else: a job checks the cards it was handed with the same
call, before it starts anything on them. A failed CUDA or nvidia-smi probe exits 2
with nothing on stdout: take no card (a failed ECC query screens nothing, as in gpu_wait).

``ahead`` -- which launches on this box come before mine, one per line; exits 1 while
any of them still waits for cards. A launch is a command started by
``register <name> --exclude <set aside> -- cmd``, which holds an flock on ``REG_DIR/<name>``
for as long as the command or anything that inherited the lock fd lives (gpu_wait's claim
holders keep it while its job runs); the kernel drops it when they are all gone, so a
finished or killed launch never lingers. Its candidate cards are the ones its waiter may pick: every CUDA
ordinal on the box minus ``--exclude``, the same string the waiter hands ``cards --exclude``.
With nothing set aside, or when they cannot be read (a registration from an older copy of
this script, or one still being written), they are every card. A launch waits for cards
until a gpu_wait that inherited its lock fd has claimed: that gpu_wait runs
``claimed <its cards>``, which records them in the registration as the cards the launch last
claimed, which the claim locks keep while that job runs; the record stays when that job
ends, until a later claim under the launch records its own. A gpu_wait copy that records
nothing (an older one) still leaves the registration open in whatever holds its claim
locks: until a record comes, the launch holds the cards whose claim lock is held by a process
that has its registration open, and waits while there are none. The
launches ahead of a registered caller are the ones registered before its own, still
waiting, whose candidate cards meet its own; for a caller outside any launch, all of them,
but it waits only for those still waiting, as if it registered now on every card. Nothing
is recognised by its command line: a process that did not register is seen only through
the cards it occupies. ``register`` exits 3 while a launch of that name is alive on the
box, 2 when ``--exclude`` leaves no card.

A runner registers its whole launch (the waiter, then the chain under one gpu_wait):
launches whose candidate cards meet queue first come, first served, a later one behind an
earlier one while that waits for cards. A later launch goes past an earlier one only when it
cannot take a card that one waits for: their candidate cards do not meet, or that one holds
its cards already, so it cannot starve it. A launch that has claimed has no place in line
again, neither between batches or stages that claim one by one nor while a second gpu_wait
under it waits: run such a series under one ``gpu_wait --hold`` to keep its cards.
A service launch script waits on ``ahead`` and does not register: once up, it holds just
its claimed card, which ``cards`` sees.
Inside gpu_wait's job the launch is not the caller's own: gpu_wait's claim holders keep
the registration and the job does not get it, so ``ahead`` asked there lists the job's
own launch. Only the waiter asks ``ahead``; a service that a launch needs is started
before that launch registers.

  launcher   X=<cards set aside>   # written once: register and the waiter's cards both get it
             setsid nohup python scripts/gpu_busy.py register <run> --exclude "$X" -- bash waiter.sh "$X" > <log> 2>&1 < /dev/null &
  waiter     X=$1; until python scripts/gpu_busy.py ahead; do sleep 120; done
             read -r FREE ALL < <(python scripts/gpu_busy.py cards --exclude "$X")   # FREE empty: probe failed; take K of FREE, gpu_wait --exclude ALL minus them
  chain      read -r B _ < <(python scripts/gpu_busy.py cards); [ -n "$B" ] || exit 1   # right after gpu_wait claimed
             for c in ${CUDA_VISIBLE_DEVICES//,/ }; do [[ ,$B, == *,$c,* ]] || exit 1; done
  service    until python scripts/gpu_busy.py ahead; do sleep 120; done; exec python scripts/gpu_wait.py ...

``cards``, and ``register`` with cards set aside, ask the CUDA driver which cards there are,
as gpu_wait does (``gpu_wait.cuda_uuids``).
"""
from __future__ import annotations

import argparse
import fcntl
import os
import signal
import subprocess
import sys
import time

import gpu_wait
from gpu_hold import BALLAST_NAME, IDLE_MB
from gpu_wait import held, key, open_files

REG_DIR = "/tmp/gpu_launches"   # box-local like gpu_wait.LOCK_DIR: flock on NFS is a lie
REGISTER_TRIES = 40             # an `ahead` probe holds a registration lock for microseconds, a live launch throughout
ALREADY_ALIVE = 3
CARDS_TAG = "cards"             # starts line 2 of a registration, its candidate cards; older copies read only line 1
HOLDS_TAG = "holds"             # starts the line ``claimed`` adds after those, the cards the launch last claimed; older copies never take it for cards


class ProbeFailed(RuntimeError):
    pass


def smi(what: str, fields: str) -> str:
    try:
        return subprocess.run(["nvidia-smi", f"--query-{what}={fields}", "--format=csv,noheader,nounits"],
                              capture_output=True, text=True, check=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError) as e:
        raise ProbeFailed(f"nvidia-smi --query-{what}: {e}") from e


def rows(text: str, n: int) -> list[list[str]]:
    """CSV rows of ``n`` fields; the last field keeps its commas (a process name)."""
    out = [[f.strip() for f in line.split(",", n - 1)] for line in text.splitlines() if line.strip()]
    if any(len(r) != n for r in out):
        raise ProbeFailed(f"unexpected nvidia-smi output: {text!r}")
    return out


def own_claim(path: str, mine: dict[tuple[int, int], int]) -> bool:
    """The claim somebody holds on claim lock ``path`` is this process's (``mine``: its open files): it has
    the lock open (the fd a gpu_wait copy hands its job) or the pipe the lock's claim holder reads."""
    if key(path) in mine:
        return True
    holder = gpu_wait.claim_holder(path)
    return holder is not None and gpu_wait.reads_pipe_of(holder, mine)


def ordinals(text: str) -> frozenset[int]:
    """Comma-separated CUDA ordinals."""
    return frozenset(int(c) for c in text.split(",") if c.strip())


def listed(cards: frozenset[int]) -> str:
    return ",".join(map(str, sorted(cards)))


def card_report(exclude: frozenset[int] = frozenset()) -> dict[int, str]:
    """``{cuda_ordinal: why it cannot be claimed}``, ``""`` for a claimable card."""
    uuid_of = gpu_wait.cuda_uuids()
    if not uuid_of:
        raise ProbeFailed("the CUDA driver enumerates no card")
    try:
        used = {u: int(mb) for u, mb in rows(smi("gpu", "uuid,memory.used"), 2)}
        apps = [(u, pid, int(mb) if mb.isdigit() else 0, name)
                for u, pid, mb, name in rows(smi("compute-apps", "gpu_uuid,pid,used_memory,process_name"), 4)]
    except ValueError as e:
        raise ProbeFailed(f"unexpected nvidia-smi output: {e}") from e
    bad = gpu_wait.ecc_bad_gpus()
    mine = open_files()
    report = {}
    for card, uuid in sorted(uuid_of.items()):
        claims = [path for path in gpu_wait.card_locks(card) if held(path)]
        ours = bool(claims) and all(own_claim(path, mine) for path in claims)
        on_card = [(pid, mb, ours and name == BALLAST_NAME, name) for u, pid, mb, name in apps if u == uuid]
        others = [f"{pid} ({name})" for pid, _, own, name in on_card if not own]
        rest_mb = used.get(uuid, 0) - sum(mb for _, mb, own, _ in on_card if own)
        if card in exclude:
            report[card] = "excluded"
        elif card in bad:
            report[card] = "uncorrectable ECC errors"
        elif uuid not in used:
            report[card] = "not listed by nvidia-smi"
        elif claims and not ours:
            report[card] = "claimed by another gpu_wait"
        elif others:
            report[card] = "in use by pid " + ", ".join(others)
        elif rest_mb > IDLE_MB:
            report[card] = f"{rest_mb} MiB used"
        else:
            report[card] = ""
    return report


def launches() -> list[tuple[str, int | None, str, frozenset[int] | None, frozenset[int] | None, tuple[int, int] | None]]:
    """Every live launch on this box: ``(name, registered at (ns), its line, its candidate
    cards (``None``: every card), the cards it holds (``None``: it waits for cards), its file)``.
    The cards it holds are the ones its holds line names, else those whose claim lock is held
    by a process that has its registration open (``claim_files``)."""
    out, claims = [], None
    for name in sorted(os.listdir(REG_DIR)) if os.path.isdir(REG_DIR) else []:
        path = os.path.join(REG_DIR, name)
        if held(path):
            with open(path, "rb") as f:
                lines = [line.decode(errors="replace") for line in f] + ["", ""]
            ns, _, rest = lines[0].strip().partition(" ")
            holds = next((h for line in lines[1:] if (h := tagged(line, HOLDS_TAG))), None)
            k = key(path)
            if holds is None:
                claims = claim_files() if claims is None else claims
                holds = frozenset(card for card, files in claims if k in files) or None
            out.append((name, int(ns) if ns.isdigit() else None, rest, tagged(lines[1], CARDS_TAG), holds, k))
    return out


def claim_files() -> list[tuple[int, dict[tuple[int, int], int]]]:
    """``(card, the files open in the process holding its claim lock)`` for every claim held on the box.
    A claim holder keeps the registration its gpu_wait inherited, and a copy that hands its job the
    lock fd leaves the job both: either way the launch's file is among them."""
    return [(card, open_files(pid)) for card, _, pid in gpu_wait.held_claims()]


def tagged(line: str, tag: str) -> frozenset[int] | None:
    """The cards on a registration line starting with ``tag``; ``None`` when there are none to read:
    another tag (an older copy wrote no cards line), or a line not fully written yet (a whole line
    ends in a newline)."""
    t, _, text = line.partition(" ")
    if t != tag or not text.endswith("\n"):
        return None
    try:
        return ordinals(text) or None
    except ValueError:
        return None


def ahead() -> list[tuple[str, int | None, str, frozenset[int] | None, frozenset[int] | None]]:
    """The launches registered before this process's own, still waiting for cards, whose
    candidate cards meet its own; all of them when it has none. One whose registration time
    cannot be read yet counts as before."""
    mine = open_files()
    regs = launches()
    own = [(ns, cards) for _, ns, _, cards, _, k in regs if k in mine]
    if not own:
        return [reg[:-1] for reg in regs]
    start, my_cards = min(own, key=lambda o: o[0])
    return [(name, ns, rest, cards, holds) for name, ns, rest, cards, holds, _ in regs
            if holds is None and (ns is None or ns < start) and (cards is None or my_cards is None or cards & my_cards)]


def claimed(cards: frozenset[int]) -> None:
    """Every launch this process belongs to holds ``cards`` now: its holds line, after what
    ``take`` wrote, takes the place of one an earlier claim left. The file is cut first, so until
    the whole new line is there a reader finds none, or one without its newline, and takes the
    launch for still waiting."""
    mine = open_files()
    for *_, k in launches():
        if k in mine:
            fd = mine[k]
            text = os.pread(fd, os.fstat(fd).st_size, 0)
            end = text.find(f"\n{HOLDS_TAG} ".encode()) + 1 or len(text)   # where an earlier claim's line starts, else the end
            os.ftruncate(fd, end)
            os.pwrite(fd, f"{HOLDS_TAG} {listed(cards)}\n".encode(), end)


def take(name: str, note: str, cards: frozenset[int] | None) -> int | None:
    """Register launch ``name`` with its candidate cards (``None``: every card): the held lock
    fd, or ``None`` while a launch of that name lives."""
    if not name or "/" in name or name in (".", ".."):
        raise ValueError(f"bad launch name {name!r}")
    os.makedirs(REG_DIR, exist_ok=True)
    fd = os.open(os.path.join(REG_DIR, name), os.O_CREAT | os.O_RDWR, 0o666)
    for _ in range(REGISTER_TRIES):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            time.sleep(0.05)
    else:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    line = f"{time.time_ns()} pid {os.getpid()}: {note}"[:300].replace("\n", " ") + "\n"
    os.write(fd, (line + ("" if cards is None else f"{CARDS_TAG} {listed(cards)}\n")).encode())
    return fd


def register(name: str, cmd: list[str], cards: frozenset[int] | None) -> None:
    fd = take(name, " ".join(cmd), cards)
    if fd is None:
        print(f"[gpu_busy] launch {name} is alive on this box; not starting another", file=sys.stderr)
        sys.exit(ALREADY_ALIVE)
    os.set_inheritable(fd, True)
    for sig in (signal.SIGPIPE, signal.SIGXFSZ):   # Python ignores these; exec would pass that on
        signal.signal(sig, signal.SIG_DFL)
    os.execvp(cmd[0], cmd)


def stamp(ns: int | None) -> str:
    return "?" if ns is None else time.strftime("%m-%d %H:%M:%S", time.localtime(ns / 1e9))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("cards", help="claimable cards, CUDA ordinals")
    p.add_argument("--exclude", default="", help="comma-separated CUDA ordinals not to take")
    p.add_argument("--why", action="store_true", help="also say on stderr why each card is out")
    sub.add_parser("ahead", help="launches on this box before mine that may take my cards; exit 1 while any still waits for cards")
    p = sub.add_parser("claimed", help="run by gpu_wait once it has claimed: the launch it runs under holds <cards>")
    p.add_argument("cards", help="comma-separated CUDA ordinals")
    p = sub.add_parser("register", help="run a command as launch <name> of this box: register <name> [--exclude <set aside>] -- cmd")
    p.add_argument("name")
    p.add_argument("--exclude", default="", help="comma-separated CUDA ordinals its waiter sets aside")
    argv = sys.argv[1:] if argv is None else argv
    cut = argv.index("--") if "--" in argv else len(argv)
    args, cmd = ap.parse_args(argv[:cut]), argv[cut + 1:]
    if args.mode == "cards":
        try:
            report = card_report(ordinals(args.exclude))
        except ProbeFailed as e:
            print(f"[gpu_busy] card probe failed, take no card: {e}", file=sys.stderr)
            sys.exit(2)
        if args.why:
            for card, why in report.items():
                print(f"[gpu_busy] CUDA {card}: {why or 'claimable'}", file=sys.stderr)
        print(",".join(str(c) for c, why in report.items() if not why) or "-", ",".join(map(str, report)))
    elif args.mode == "ahead":
        before = ahead()
        for name, ns, rest, cards, holds in before:
            where = f"claimed cards {listed(holds)}" if holds else f"cards {listed(cards)}" if cards else "every card"
            print(f"{name} (registered {stamp(ns)}, {where}, {rest})")
        sys.exit(1 if any(holds is None for *_, holds in before) else 0)
    elif args.mode == "claimed":
        claimed(ordinals(args.cards))
    else:
        if not cmd:
            ap.error("no command given after --")
        exclude = ordinals(args.exclude)
        cards = frozenset(gpu_wait.cuda_uuids()) - exclude if exclude else None
        if cards is not None and not cards:
            print(f"[gpu_busy] no CUDA ordinal outside --exclude {args.exclude}; not starting {args.name}", file=sys.stderr)
            sys.exit(2)
        register(args.name, cmd, cards)


if __name__ == "__main__":
    main()

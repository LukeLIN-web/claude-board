"""Read ~/.claude/sessions/*.json and enrich each with TTY + project metadata."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterator, NamedTuple, Optional

def _home_base() -> Path:
    """Base dir the dashboard reads from. Override with CLAUDE_FLEET_HOME to
    point at a fixture/demo tree (used for screenshots, demos, and tests)."""
    env = os.environ.get("CLAUDE_FLEET_HOME")
    return Path(env).expanduser() if env else Path.home()


HOME_BASE = _home_base()
CLAUDE_HOME = HOME_BASE / ".claude"
SESSIONS_DIR = CLAUDE_HOME / "sessions"
PROJECTS_DIR = CLAUDE_HOME / "projects"

# Where core/usage.py runs the throwaway claude it reads /usage from. It is a
# live claude with a session file like any other, so discovery would card it for
# the seconds it lives; `_cwd_visible` keeps it off the board instead. Inside the
# repo so it inherits whatever folder trust the board's own directory has.
PROBE_CWD = Path(__file__).resolve().parents[1] / ".usage-probe"


def _cwd_to_project_slug(cwd: str) -> str:
    """Mirror Claude Code's project-dir naming: / _ . all become -"""
    return cwd.replace("/", "-").replace("_", "-").replace(".", "-")


def _is_hidden_cwd(cwd: str) -> bool:
    """Hide internal agent sub-sessions: SDK-spawned agents live under a
    `.slock/agents/...` working dir and are noise on the dashboard, not real
    user windows."""
    return ".slock" in Path(cwd).parts


def _parse_prefixes(env_value: str) -> list[str]:
    """Split a colon/comma-separated list of path prefixes into normalized
    absolute paths. Blank entries are dropped."""
    out: list[str] = []
    for chunk in env_value.replace(",", ":").split(":"):
        chunk = chunk.strip()
        if chunk:
            out.append(os.path.normpath(os.path.expanduser(chunk)))
    return out


# Machine-local visibility filter (default: show everything, so any host that
# leaves these env vars unset is unaffected). Set them per-host — e.g. in a
# gitignored .env.local sourced by run.sh — not in committed code.
#   CLAUDE_FLEET_CWD_INCLUDE — if set, only sessions whose cwd is under one of
#                             these path prefixes are shown.
#   CLAUDE_FLEET_CWD_EXCLUDE — sessions under any of these prefixes are hidden.
# Exclude wins over include. Both are colon/comma-separated prefix lists.
_CWD_INCLUDE: list[str] = []
_CWD_EXCLUDE: list[str] = []
# Slugified mirrors (cwd → project-dir name), so callers that only have the
# `projects/<slug>` dir name (e.g. search hits) can apply the same filter.
_CWD_INCLUDE_SLUGS: list[str] = []
_CWD_EXCLUDE_SLUGS: list[str] = []


def _reload_cwd_filters() -> None:
    """(Re)read the cwd filter env vars. Called at import; exposed for tests."""
    global _CWD_INCLUDE, _CWD_EXCLUDE, _CWD_INCLUDE_SLUGS, _CWD_EXCLUDE_SLUGS
    _CWD_INCLUDE = _parse_prefixes(os.environ.get("CLAUDE_FLEET_CWD_INCLUDE", ""))
    _CWD_EXCLUDE = _parse_prefixes(os.environ.get("CLAUDE_FLEET_CWD_EXCLUDE", ""))
    _CWD_INCLUDE_SLUGS = [_cwd_to_project_slug(p) for p in _CWD_INCLUDE]
    _CWD_EXCLUDE_SLUGS = [_cwd_to_project_slug(p) for p in _CWD_EXCLUDE]


_reload_cwd_filters()


def _under(cwd: str, prefix: str) -> bool:
    cwd_n = os.path.normpath(cwd)
    # rstrip: the root prefix "/" already ends in the separator, and "//" would
    # match nothing — CLAUDE_FLEET_CWD_INCLUDE=/ hid every session.
    return cwd_n == prefix or cwd_n.startswith(prefix.rstrip(os.sep) + os.sep)


def _cwd_visible(cwd: str) -> bool:
    """Whether a session with this working dir passes the machine-local filter.
    The board's own usage probe (core/usage.py) never does, whatever the filter."""
    if cwd and _under(cwd, str(PROBE_CWD)):
        return False
    if _CWD_EXCLUDE and any(_under(cwd, p) for p in _CWD_EXCLUDE):
        return False
    if _CWD_INCLUDE and not any(_under(cwd, p) for p in _CWD_INCLUDE):
        return False
    return True


def _slug_under(slug: str, prefix_slug: str) -> bool:
    return slug == prefix_slug or slug.startswith(prefix_slug.rstrip("-") + "-")


def slug_visible(slug: str) -> bool:
    """Same filter as `_cwd_visible`, but for a `projects/<slug>` dir name.

    The slug is a lossy encoding of the cwd (/ _ . all become -), so this
    over-matches — not only `a/b` vs `a_b`, but every sibling whose name extends
    an included dir with "-": `proj` lets `proj-evil` through. Prefer
    `transcript_visible` wherever there is a transcript to read."""
    if _CWD_EXCLUDE_SLUGS and any(_slug_under(slug, p) for p in _CWD_EXCLUDE_SLUGS):
        return False
    if _CWD_INCLUDE_SLUGS and not any(_slug_under(slug, p) for p in _CWD_INCLUDE_SLUGS):
        return False
    return True


# transcript path -> the cwd its rows record. A transcript never changes
# directory, so a hit is kept for good; a file with no cwd row yet is re-read.
_TRANSCRIPT_CWD: dict[str, str] = {}


def _transcript_cwd(path: Path) -> str:
    """The cwd a transcript was written from, "" if no row names one yet. Claude
    puts it on its rows; a Codex rollout, in the payload of the session_meta it
    opens with."""
    key = str(path)
    if key in _TRANSCRIPT_CWD:
        return _TRANSCRIPT_CWD[key]
    cwd = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for _, line in zip(range(200), f):
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict) and d.get("type") == "session_meta":
                    d = d.get("payload")
                c = d.get("cwd") if isinstance(d, dict) else None
                if isinstance(c, str) and c:
                    cwd = c
                    break
    except OSError:
        return ""
    if cwd:
        _TRANSCRIPT_CWD[key] = cwd
    return cwd


def transcript_visible(path: str | Path) -> bool:
    """Whether a Claude transcript's or Codex rollout's session passes the
    machine-local filter.

    Decided on the cwd the transcript records, so it is exactly `_cwd_visible`:
    the slug of its projects/ dir cannot tell /w/proj-evil from /w/proj/evil,
    and an allowlist read off the slug served /w/proj-evil's whole timeline.
    The slug is only the fallback for a file whose rows name no cwd yet — for a
    rollout that is a date dir, which no prefix covers, so an allowlist hides it
    and an exclude list alone does not."""
    if not (_CWD_INCLUDE or _CWD_EXCLUDE):
        return True
    p = Path(path)
    cwd = _transcript_cwd(p)
    return _cwd_visible(cwd) if cwd else slug_visible(p.parent.name)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _ps_tty(raw: str) -> Optional[str]:
    """The device a `ps` tty column names, or None for a process with no
    controlling terminal ("?", "??") — a background job, not a window."""
    return f"/dev/{raw}" if raw and raw not in ("?", "??") else None


def _proc_cwd(pid: int) -> str:
    """The directory `pid` runs in, "" where /proc can't say."""
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def _proc_start_ms(pid: int) -> int:
    """Immutable process start time (ms) from the /proc/<pid> dir mtime; 0 if the
    process is gone. Constant for the life of the pid, so it makes a stable card-
    ordering anchor — see the started_at note in list_windows."""
    try:
        return int(os.stat(f"/proc/{pid}").st_mtime * 1000)
    except Exception:
        return 0


class Proc(NamedTuple):
    """One process, as `ps` lists it."""
    ppid: int
    stat: str   # raw ps STAT; its first letter is the scheduler state ('D' == uninterruptible sleep)
    tty: str    # controlling terminal, e.g. "pts/3"; "?" (Linux) / "??" (macOS) for none
    comm: str   # process name — what counts as a shell
    args: str   # the whole command line


# ucomm leads because it is the one column besides args that can hold spaces
# ("tmux: server"), and args has to come last since it runs to the end of the
# line — so a row is split on the pid/ppid/STAT that follow the name. ucomm is
# the process name: `comm` on Linux, while macOS's `comm` is the executable's
# path, which ps cuts to 16 characters anywhere but the last column.
_PS_FORMAT = "ucomm=,pid=,ppid=,stat=,tty=,args="
_PS_ROW = re.compile(r"^(.*?)\s+(\d+)\s+(\d+)\s+([A-Z]\S*)\s+(\S+)(?:\s+(.*))?$")

# One `ps` pass serves every reader of the process table in a tick — process-
# first Claude, Codex and hmz discovery, the cards' ttys, the shell counts —
# where each used to fork its own `ps -e` (~55 ms apiece on a busy host). The
# 2s watcher tick reads it fresh; whatever else runs within the second shares it.
_PROC_TTL = 1.0
_proc_cache: tuple[float, dict[int, Proc]] = (float("-inf"), {})


def _clear_caches() -> None:
    """Forget the cached process table, so the next read runs `ps` (tests that
    stub `ps` call this first)."""
    global _proc_cache
    _proc_cache = (float("-inf"), {})


def proc_table(max_age: float = _PROC_TTL) -> dict[int, Proc]:
    """{pid: Proc} for every process, in ps order, from one `ps` call; {} on any
    failure (e.g. `ps` unavailable). A table read within `max_age` seconds is
    reused; it is shared, so callers must not change it."""
    global _proc_cache
    now = time.monotonic()
    ts, table = _proc_cache
    if now - ts < max_age:
        return table
    try:
        out = subprocess.check_output(
            ["ps", "-eo", _PS_FORMAT],
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode("utf-8", "replace")
    except Exception:
        out = ""
    table = {}
    for line in out.splitlines():
        m = _PS_ROW.match(line)
        if m:
            comm, pid, ppid, stat, tty, args = m.groups()
            table[int(pid)] = Proc(int(ppid), stat, tty,
                                   comm.strip().rsplit("/", 1)[-1], args or "")
    _proc_cache = (now, table)
    return table


def _dev_tty(table: dict[int, Proc], pid: int) -> Optional[str]:
    """`/dev/<tty>` of `pid`, None when it isn't in the table. The rule is the
    one `ps -o tty= -p <pid>` was read with here: only macOS's "??" means no
    terminal, so a Linux pid without one reads as "/dev/?"."""
    p = table.get(pid)
    if not p or not p.tty or p.tty == "??":
        return None
    return f"/dev/{p.tty}"


# Process names treated as a "shell" when counting background shells per session.
_SHELL_COMMS = {"bash", "sh", "zsh", "dash", "fish", "ksh", "tcsh", "csh", "ash"}


def _children(table: dict[int, Proc]) -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    for pid, p in table.items():
        children.setdefault(p.ppid, []).append(pid)
    return children


def _subtree(children: dict[int, list[int]], root: int) -> Iterator[int]:
    """`root` and every process under it, each once."""
    stack = [root]
    seen: set[int] = set()
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        yield cur
        stack.extend(children.get(cur, []))


def shell_descendant_counts(pids: list[int]) -> dict[int, int]:
    """Count descendant shell processes for each pid from one process table.

    Walks the process tree and, for every requested pid, counts how many of its
    descendants are shell processes (bash/sh/zsh/...). Used to show how many
    background shells a Claude Code session currently has running.
    Returns {pid: count}; all-zero on any failure (e.g. `ps` unavailable).
    """
    targets = set(pids)
    if not targets:
        return {}
    table = proc_table()
    children = _children(table)
    return {pid: sum(1 for cur in _subtree(children, pid)
                     if cur != pid and table[cur].comm in _SHELL_COMMS)
            for pid in targets}


def uninterruptible_wrappers(claude_pid: int) -> list[int]:
    """Direct shell children of `claude_pid` whose subtree holds a D-state
    (uninterruptible-sleep) process.

    This is the exact — and only — case where the dashboard's Esc can never
    work. Claude Code's interrupt sends SIGINT to the Bash tool it spawned, but
    a `bash -c` blocked in wait() on a foreground child ignores SIGINT and
    forwards it to that child, and a D-state child (e.g. an nvidia-smi stuck on
    a wedged GPU driver) is immune to every signal, SIGKILL included. That
    wrapper bash is the reap target Claude Code awaits, so SIGKILL-ing *it* —
    it sits in killable, interruptible do_wait — resolves the turn while the D
    child harmlessly reparents to init.

    Restricting to shells with a D descendant is the safety gate: a normal
    long-running command (which Esc *can* interrupt) is left alone, and a
    non-shell child such as the codex mcp-server is never targeted. The table is
    read fresh rather than taken from the tick's cache: what this returns gets
    SIGKILLed.
    """
    table = proc_table(max_age=0)
    children = _children(table)
    return [child for child in children.get(claude_pid, [])
            if table[child].comm in _SHELL_COMMS
            and any(table[cur].stat.startswith("D") for cur in _subtree(children, child))]


@dataclass
class Window:
    pid: int
    session_id: str
    cwd: str
    project_name: str
    project_slug: str
    name: Optional[str]
    status: str           # busy | idle | waiting
    waiting_for: Optional[str]
    started_at: int       # ms
    updated_at: int       # ms
    version: str
    tty: Optional[str]
    transcript_path: Optional[str]
    alive: bool
    hidden: bool          # internal `.slock` agent sub-session, shown at page bottom
    platform: str = "claude"   # "claude" | "codex" | "hmz" — which CLI owns this window

    def to_dict(self) -> dict:
        d = asdict(self)
        d["idle_seconds"] = max(0, int(time.time() - self.updated_at / 1000))
        return d


def _load_session_file(path: Path) -> Optional[dict]:
    try:
        with path.open() as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict) or "pid" not in data:
        return None
    return data


def list_windows(include_dead: bool = False) -> list[Window]:
    if not SESSIONS_DIR.exists():
        return []

    windows: list[Window] = []
    table = proc_table()

    for f in SESSIONS_DIR.glob("*.json"):
        # Skip the legacy `session-{ts}.json` files (no pid).
        if f.name.startswith("session-"):
            continue
        data = _load_session_file(f)
        if not data:
            continue

        cwd = data.get("cwd", "")
        # Machine-local visibility filter (CLAUDE_FLEET_CWD_INCLUDE/EXCLUDE).
        if not _cwd_visible(cwd):
            continue

        pid = int(data["pid"])
        alive = _pid_alive(pid)
        if not alive and not include_dead:
            continue

        session_id = data.get("sessionId", "")
        hidden = _is_hidden_cwd(cwd)
        slug = _cwd_to_project_slug(cwd)
        transcript = PROJECTS_DIR / slug / f"{session_id}.jsonl"

        # Card-ordering anchor: pin a live session to its immutable process start
        # time, the SAME value list_claude_proc_windows seeds before the session
        # file exists. A fresh spawn is first carded process-first (proc start),
        # then from its file once written — if those two clocks disagree the card
        # jumps slots on that handoff. Claude's recorded `startedAt` ≈ proc start
        # for a clean launch but is the ORIGINAL session time on `--resume`, hours
        # off; proc start keeps the card put either way. Dead/historical windows
        # have no /proc, so fall back to the file's startedAt. Codex pins the same
        # way (core/codex.py). started_at is sort-only; it is never displayed.
        started_at = (_proc_start_ms(pid) if alive else 0) or int(
            data.get("startedAt", 0))

        windows.append(
            Window(
                pid=pid,
                session_id=session_id,
                cwd=cwd,
                project_name=os.path.basename(cwd) or cwd,
                project_slug=slug,
                name=data.get("name"),
                status=data.get("status", "unknown"),
                waiting_for=data.get("waitingFor"),
                started_at=started_at,
                # `.slock` agent sub-sessions only write `startedAt` (no
                # `updatedAt` heartbeat); fall back so idle isn't computed
                # from the epoch (which renders as ~494593h ago).
                updated_at=int(data.get("updatedAt") or data.get("startedAt", 0)),
                version=str(data.get("version", "")),
                tty=_dev_tty(table, pid) if alive else None,
                transcript_path=str(transcript) if transcript.exists() else None,
                alive=alive,
                hidden=hidden,
            )
        )

    # Newest activity first.
    windows.sort(key=lambda w: (-w.updated_at, w.pid))
    return windows


# Claude CLI subcommands / flags that are headless (no interactive TUI) and so
# must never earn a card: `claude mcp …`, `claude -p/--print …` (scripted runs).
_CLAUDE_BG_SUBCOMMANDS = {"mcp", "config", "doctor", "update", "install", "migrate-installer"}
# The options whose value is the session id a resumed Claude is on.
_CLAUDE_RESUME_FLAGS = frozenset({"--resume", "-r", "--continue", "-c"})


def _exe_index(tokens: list[str], name: str) -> int:
    """Index of the `name` executable token (`claude`, `codex`), or -1. Covers
    both a bare `name …` and a node launcher (`node …/bin/name …`): the
    executable is among the first two tokens."""
    for i, t in enumerate(tokens[:2]):
        if os.path.basename(t) == name:
            return i
    return -1


def _cli_words(args: str | list[str], exe: str,
               takes_value: frozenset[str]) -> Optional[tuple[str, list[tuple[str, str]]]]:
    """How `exe` was run, read off a `ps` args string or the real argv (see
    _proc_argv): (first word, options), or None when the command isn't `exe`.

    The first word is the first argument that is neither an option nor the
    value of one in `takes_value` — the subcommand, when it names one; "" when
    there is none. Only that word can name a subcommand: past it is the opening
    prompt, and `claude "fix the mcp config"` is a TUI like any other. Each
    option comes with the value it took, "" for a flag. A value is the next
    argument, unless that is an option itself (`--resume` alone opens a
    picker); `--opt=value` and `-oVALUE` are one argument, so they read as a
    flag."""
    toks = args.split() if isinstance(args, str) else list(args)
    i = _exe_index(toks, exe)
    if i < 0:
        return None
    rest = toks[i + 1:]
    first: Optional[str] = None
    opts: list[tuple[str, str]] = []
    j = 0
    while j < len(rest):
        t = rest[j]
        j += 1
        if t in takes_value and j < len(rest) and not rest[j].startswith("-"):
            opts.append((t, rest[j]))
            j += 1
        elif t.startswith("-"):
            opts.append((t, ""))
        elif first is None:
            first = t
    return first or "", opts


def _parse_claude_proc(args: str | list[str]) -> Optional[dict]:
    """Classify a process command line — a `ps` args string, or the real argv
    when there is one (see _proc_argv). Returns {session_id} for an interactive
    Claude TUI process (resume id parsed when present), or None otherwise."""
    cli = _cli_words(args, "claude", _CLAUDE_RESUME_FLAGS)
    if cli is None:
        return None
    first, opts = cli
    if any(o in ("-p", "--print") for o, _ in opts):
        return None  # headless scripted run, not a TUI
    if first in _CLAUDE_BG_SUBCOMMANDS:
        return None  # `claude mcp`, `claude config`, … → headless
    session_id = next((v for o, v in reversed(opts) if o in _CLAUDE_RESUME_FLAGS and v), "")
    return {"session_id": session_id}


def _proc_argv(pid: int, args: str) -> str | list[str]:
    """`pid`'s real argv from /proc, or `args` back when it can't be had.

    `ps -o args` joins argv with spaces, so an opening prompt reads as loose
    words — and one of them can be `-p`, which made `claude "explain what -p
    does"` look like a scripted run. /proc keeps the arguments apart. It is only
    trusted while it still joins to exactly what ps printed: by now the pid may
    have exited and been reused, and a process may have rewritten its own argv.
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return args
    argv = raw.decode("utf-8", "replace").split("\0")
    if argv and argv[-1] == "":
        argv.pop()
    return argv if " ".join(argv) == args else args


def _parse_ps_claude(pid: int, args: str) -> Optional[dict]:
    """_parse_claude_proc for one `ps` row, on the real argv when it's a claude."""
    if _exe_index(args.split(), "claude") < 0:
        return None  # not claude: no /proc read for every process on the host
    return _parse_claude_proc(_proc_argv(pid, args))


def _discover_proc_transcript(slug: str, start_ms: int, claimed_sids: set[str]):
    """Link a process-discovered Claude session that has NO `--resume` id (a
    fresh spawn) to the transcript it is writing, so the card and the prompt
    queue aren't permanently blank.

    A fresh `claude` writes no `~/.claude/sessions/<pid>.json` in some setups, so
    its only on-disk trace is the transcript it starts once it completes its
    first turn. That file lives in this cwd's project dir (the dir *is* the cwd
    filter — transcript records carry no `cwd` field) and is modified at/after
    the process started. Pick the newest such file not already claimed by another
    window. Returns (session_id, path) or (None, None) when nothing matches yet —
    e.g. the session is still parked on a menu and hasn't written a turn.
    """
    proj = PROJECTS_DIR / slug
    if not proj.is_dir():
        return None, None
    best = None  # (mtime_ms, session_id, path)
    try:
        candidates = list(proj.glob("*.jsonl"))
    except Exception:
        return None, None
    for f in candidates:
        sid = f.stem
        if sid in claimed_sids:
            continue
        try:
            mtime_ms = f.stat().st_mtime * 1000
        except OSError:
            continue
        # A 2-min grace absorbs /proc start-time vs first-write skew; anything
        # older than that belongs to an earlier session in the same cwd.
        if mtime_ms < start_ms - 120_000:
            continue
        if best is None or mtime_ms > best[0]:
            best = (mtime_ms, sid, str(f))
    if best is None:
        return None, None
    return best[1], best[2]


def list_claude_proc_windows(
    known_pids: set[int], known_ttys: set[str], known_sids: set[str] = frozenset()
) -> list[Window]:
    """Discover running interactive `claude` processes that have NOT yet written
    a `~/.claude/sessions/<pid>.json` file — a freshly spawned session, or a
    `claude --resume <id>` parked on Claude's "resume from summary?" picker, both
    of which register no session file until the session actually starts. Without
    this they'd be invisible on the dashboard. Keyed by the live pid; dedup'd
    against the file-based windows by pid and tty so a session that has written
    its file is never double-carded. Linux-only (reads /proc); [] elsewhere.
    """
    if not Path("/proc").is_dir():
        return []
    # Every `claude` TUI process, in ps order, with what its command line says —
    # read off the real argv (_parse_ps_claude), since ps's space-joined args
    # can't tell an opening prompt from flags and subcommands.
    procs = [(pid, p.tty, parsed) for pid, p in proc_table().items()
             if (parsed := _parse_ps_claude(pid, p.args)) is not None]

    windows: list[Window] = []
    seen_ttys: set[str] = set(known_ttys)
    # Transcripts already owned by a file-based window (or an earlier proc here)
    # are off-limits, so a fresh spawn can't steal another session's transcript.
    claimed_sids: set[str] = set(known_sids)
    # Pre-claim every live `claude --resume <id>` transcript up front: those
    # resumed sessions are themselves process-discovered here (no session file,
    # so absent from known_sids), and the ps scan order is arbitrary — without
    # this a fresh spawn processed first would adopt a resumed session's
    # transcript (its file's mtime is refreshed by the resume itself).
    claimed_sids.update(parsed["session_id"] for _, _, parsed in procs if parsed["session_id"])
    for pid, tty_raw, parsed in procs:
        tty = _ps_tty(tty_raw)
        if not tty:
            continue  # no controlling terminal → background/daemon, not a window
        if pid in known_pids:
            continue  # already carded from its session file
        if tty in seen_ttys:
            continue  # one card per terminal; file-based window or earlier proc wins
        if not _pid_alive(pid):
            continue

        cwd = _proc_cwd(pid)
        if not _cwd_visible(cwd):
            continue
        seen_ttys.add(tty)

        session_id = parsed["session_id"]
        slug = _cwd_to_project_slug(cwd)
        start = _proc_start_ms(pid) or int(time.time() * 1000)

        # The newest unclaimed transcript written since this process started.
        disc_sid, disc_path = _discover_proc_transcript(slug, start, claimed_sids)
        if session_id:
            transcript = PROJECTS_DIR / slug / f"{session_id}.jsonl"
            # Recent Claude FORKS a new session id on `--resume <id>`: the
            # resume-arg file then freezes at the pre-resume point while the live
            # conversation lands in the new id, so the card would track a stale
            # file forever. If a sibling transcript was written after this process
            # started AND is newer than the resume-arg file, adopt it as the fork.
            # (The resume-arg id is pre-claimed above, so discovery returns the
            # fork, not the frozen original; None when the session simply appends
            # to the resume-arg file, the older-Claude behavior.)
            if disc_sid:
                try:
                    forked = (not transcript.exists()) or (
                        Path(disc_path).stat().st_mtime > transcript.stat().st_mtime)
                except OSError:
                    forked = True
                if forked:
                    session_id, transcript = disc_sid, Path(disc_path)
        elif disc_sid:
            # Fresh spawn (no --resume id, no session file): recover the
            # transcript it's writing so the card and prompt-queue reconciliation
            # have something to read instead of a permanent blank.
            session_id, transcript = disc_sid, Path(disc_path)
        else:
            transcript = None
        if session_id:
            claimed_sids.add(session_id)
        # Prefer the (resumed/discovered) transcript's mtime as the activity time
        # when it already exists; otherwise fall back to the process start time.
        updated = start
        if transcript and transcript.exists():
            try:
                updated = int(transcript.stat().st_mtime * 1000)
            except Exception:
                pass

        windows.append(Window(
            pid=pid,
            session_id=session_id or f"claude-{pid}",
            cwd=cwd,
            project_name=os.path.basename(cwd) or (cwd or f"claude-{pid}"),
            project_slug=slug,
            name=None,
            # Seed as a verifiable "dialog open": _enriched_snapshot checks the
            # pane and keeps it waiting when a menu is really up (e.g. the resume
            # "summary vs full" picker — genuinely waiting on the user), or flips
            # it to busy when the pane shows no menu (a session already running).
            status="waiting",
            waiting_for="dialog open",
            started_at=start,
            updated_at=updated,
            version="",
            tty=tty,
            transcript_path=str(transcript) if transcript and transcript.exists() else None,
            alive=True,
            hidden=_is_hidden_cwd(cwd),
            platform="claude",
        ))
    return windows


def _all_windows() -> Iterator[Window]:
    """Every window the board can resolve, cheapest source first and lazily, so
    a lookup that hits early never pays for the process discovery after it."""
    yield from list_windows(include_dead=True)
    # Freshly spawned / resume-picker Claude sessions aren't backed by a
    # ~/.claude/sessions file yet — resolve them from the live process so the
    # card's actions (timeline, menu, prompt, keys, close) work, not just the
    # card's display, and so a `claude --resume <id>` parked on the summary
    # picker resolves by id for resume/fork/locate. Empty known-sets ⇒ no dedup.
    yield from list_claude_proc_windows(set(), set())
    # Live Codex and hmz sessions aren't backed by ~/.claude/sessions files;
    # they're discovered from running processes. Late import to avoid a circular
    # dependency (both import HOME_BASE from this module).
    from . import codex, hmz
    for discover in (codex.list_codex_windows, hmz.list_hmz_windows):
        try:
            yield from discover()
        except Exception:
            pass


def find_window(pid: int) -> Optional[Window]:
    return next((w for w in _all_windows() if w.pid == pid), None)


def find_window_by_session(session_id: str) -> Optional[Window]:
    """Resolve a window by its Claude/Codex/hmz session id, or a unique prefix.

    This is the reverse lookup of `find_window`: humans and external tools
    (skills, monitors, scripts) usually hold a session id — e.g. from a
    transcript filename — not a pid. Prefixes must be >= 8 chars to avoid
    accidental matches; an ambiguous prefix resolves to nothing rather than
    to the wrong session.
    """
    sid = (session_id or "").strip().lower()
    if not sid:
        return None

    prefix_matches: list[Window] = []
    for w in _all_windows():
        wid = (w.session_id or "").lower()
        if wid == sid:
            return w
        if len(sid) >= 8 and wid.startswith(sid):
            prefix_matches.append(w)
    if len(prefix_matches) == 1:
        return prefix_matches[0]
    return None


def snapshot() -> dict:
    """Top-level state for the dashboard. The header `counts` are the app
    layer's (app._finalize_snapshot): they go by triage, which it attaches."""
    wins = list_windows()
    # Surface live `claude` processes that haven't registered a session file yet
    # (fresh spawns / resume parked on the summary picker) so they still card.
    known_pids = {w.pid for w in wins}
    known_ttys = {w.tty for w in wins if w.tty}
    known_sids = {w.session_id for w in wins if w.session_id}
    wins.extend(list_claude_proc_windows(known_pids, known_ttys, known_sids))
    return {
        "windows": [w.to_dict() for w in wins],
        "ts": int(time.time() * 1000),
    }

"""Read ~/.claude/sessions/*.json and enrich each with TTY + project metadata."""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

def _home_base() -> Path:
    """Base dir the dashboard reads from. Override with CLAUDE_FLEET_HOME to
    point at a fixture/demo tree (used for screenshots, demos, and tests)."""
    env = os.environ.get("CLAUDE_FLEET_HOME")
    return Path(env).expanduser() if env else Path.home()


HOME_BASE = _home_base()
CLAUDE_HOME = HOME_BASE / ".claude"
SESSIONS_DIR = CLAUDE_HOME / "sessions"
PROJECTS_DIR = CLAUDE_HOME / "projects"


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
    return cwd_n == prefix or cwd_n.startswith(prefix + os.sep)


def _cwd_visible(cwd: str) -> bool:
    """Whether a session with this working dir passes the machine-local filter."""
    if _CWD_EXCLUDE and any(_under(cwd, p) for p in _CWD_EXCLUDE):
        return False
    if _CWD_INCLUDE and not any(_under(cwd, p) for p in _CWD_INCLUDE):
        return False
    return True


def _slug_under(slug: str, prefix_slug: str) -> bool:
    return slug == prefix_slug or slug.startswith(prefix_slug + "-")


def slug_visible(slug: str) -> bool:
    """Same filter as `_cwd_visible`, but for a `projects/<slug>` dir name.

    The slug is a lossy encoding of the cwd (/ _ . all become -), so this can
    over-match in rare cases (e.g. `a/b` vs `a_b`); good enough for hiding
    search hits from filtered projects."""
    if _CWD_EXCLUDE_SLUGS and any(_slug_under(slug, p) for p in _CWD_EXCLUDE_SLUGS):
        return False
    if _CWD_INCLUDE_SLUGS and not any(_slug_under(slug, p) for p in _CWD_INCLUDE_SLUGS):
        return False
    return True


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    return True


def _proc_start_ms(pid: int) -> int:
    """Immutable process start time (ms) from the /proc/<pid> dir mtime; 0 if the
    process is gone. Constant for the life of the pid, so it makes a stable card-
    ordering anchor — see the started_at note in list_windows."""
    try:
        return int(os.stat(f"/proc/{pid}").st_mtime * 1000)
    except Exception:
        return 0


def _pid_tty(pid: int) -> Optional[str]:
    try:
        out = subprocess.check_output(
            ["ps", "-o", "tty=", "-p", str(pid)],
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).decode().strip()
    except Exception:
        return None
    if not out or out == "??":
        return None
    return f"/dev/{out}"


_TTY_CACHE: dict[int, Optional[str]] = {}

# Process names treated as a "shell" when counting background shells per session.
_SHELL_COMMS = {"bash", "sh", "zsh", "dash", "fish", "ksh", "tcsh", "csh", "ash"}


def shell_descendant_counts(pids: list[int]) -> dict[int, int]:
    """Count descendant shell processes for each pid via a single `ps` call.

    Walks the full process tree once and, for every requested pid, counts how
    many of its descendants are shell processes (bash/sh/zsh/...). Used to show
    how many background shells a Claude Code session currently has running.
    Returns {pid: count}; all-zero on any failure (e.g. `ps` unavailable).
    """
    targets = set(pids)
    if not targets:
        return {}
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid=,ppid=,comm="],
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode("utf-8", "replace")
    except Exception:
        return {pid: 0 for pid in targets}

    children: dict[int, list[int]] = {}
    comm: dict[int, str] = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            cpid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        comm[cpid] = parts[2].rsplit("/", 1)[-1]
        children.setdefault(ppid, []).append(cpid)

    result: dict[int, int] = {}
    for pid in targets:
        count = 0
        stack = list(children.get(pid, []))
        seen: set[int] = set()
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            if comm.get(cur, "") in _SHELL_COMMS:
                count += 1
            stack.extend(children.get(cur, []))
        result[pid] = count
    return result


def get_tty(pid: int) -> Optional[str]:
    if pid not in _TTY_CACHE:
        _TTY_CACHE[pid] = _pid_tty(pid)
    return _TTY_CACHE[pid]


def _prune_tty_cache(live_pids: set[int]) -> None:
    for pid in list(_TTY_CACHE.keys()):
        if pid not in live_pids:
            _TTY_CACHE.pop(pid, None)


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
    platform: str = "claude"   # "claude" | "codex" — which CLI owns this window

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
    live_pids: set[int] = set()

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
        if alive:
            live_pids.add(pid)
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
                tty=get_tty(pid) if alive else None,
                transcript_path=str(transcript) if transcript.exists() else None,
                alive=alive,
                hidden=hidden,
            )
        )

    _prune_tty_cache(live_pids)

    # Newest activity first.
    windows.sort(key=lambda w: (-w.updated_at, w.pid))
    return windows


# Claude CLI subcommands / flags that are headless (no interactive TUI) and so
# must never earn a card: `claude mcp …`, `claude -p/--print …` (scripted runs).
_CLAUDE_BG_SUBCOMMANDS = {"mcp", "config", "doctor", "update", "install", "migrate-installer"}


def _claude_exe_index(tokens: list[str]) -> int:
    """Index of the `claude` executable token, or -1. Covers both a bare
    `claude …` and a node launcher (`node …/bin/claude …`)."""
    for i, t in enumerate(tokens[:2]):
        if os.path.basename(t) == "claude":
            return i
    return -1


def _parse_claude_proc(args: str) -> Optional[dict]:
    """Classify a process command line. Returns {session_id} for an interactive
    Claude TUI process (resume id parsed when present), or None otherwise."""
    toks = args.split()
    i = _claude_exe_index(toks)
    if i < 0:
        return None
    rest = toks[i + 1:]
    session_id = ""
    j = 0
    while j < len(rest):
        t = rest[j]
        if t in ("-p", "--print"):
            return None  # headless scripted run, not a TUI
        if t in ("--resume", "-r", "--continue", "-c"):
            if j + 1 < len(rest) and not rest[j + 1].startswith("-"):
                session_id = rest[j + 1]
                j += 2
                continue
        elif not t.startswith("-"):
            if t in _CLAUDE_BG_SUBCOMMANDS:
                return None  # `claude mcp`, `claude config`, … → headless
        j += 1
    return {"session_id": session_id}


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
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid=,tty=,args="],
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode("utf-8", "replace")
    except Exception:
        return []

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
    for _line in out.splitlines():
        _parts = _line.split(None, 2)
        if len(_parts) >= 3:
            _p = _parse_claude_proc(_parts[2])
            if _p and _p.get("session_id"):
                claimed_sids.add(_p["session_id"])
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        tty_raw, args = parts[1], parts[2]
        if tty_raw in ("?", "??") or not tty_raw:
            continue  # no controlling terminal → background/daemon, not a window
        if pid in known_pids:
            continue  # already carded from its session file
        parsed = _parse_claude_proc(args)
        if parsed is None:
            continue
        tty = f"/dev/{tty_raw}"
        if tty in seen_ttys:
            continue  # one card per terminal; file-based window or earlier proc wins
        if not _pid_alive(pid):
            continue

        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except Exception:
            cwd = ""
        if not _cwd_visible(cwd):
            continue
        seen_ttys.add(tty)

        session_id = parsed["session_id"]
        slug = _cwd_to_project_slug(cwd)
        start = _proc_start_ms(pid) or int(time.time() * 1000)

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
            disc_sid, disc_path = _discover_proc_transcript(slug, start, claimed_sids)
            if disc_sid:
                try:
                    forked = (not transcript.exists()) or (
                        Path(disc_path).stat().st_mtime > transcript.stat().st_mtime)
                except OSError:
                    forked = True
                if forked:
                    session_id, transcript = disc_sid, Path(disc_path)
        else:
            # Fresh spawn (no --resume id, no session file): recover the
            # transcript it's writing so the card and prompt-queue reconciliation
            # have something to read instead of a permanent blank.
            disc_sid, disc_path = _discover_proc_transcript(slug, start, claimed_sids)
            if disc_sid:
                session_id = disc_sid
                transcript = Path(disc_path)
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


def find_window(pid: int) -> Optional[Window]:
    for w in list_windows(include_dead=True):
        if w.pid == pid:
            return w
    # Freshly spawned / resume-picker Claude sessions aren't backed by a
    # ~/.claude/sessions file yet — resolve them from the live process so the
    # card's actions (timeline, menu, prompt, keys, close) work, not just the
    # card's display. Empty known-sets ⇒ no dedup; we already missed above.
    for w in list_claude_proc_windows(set(), set()):
        if w.pid == pid:
            return w
    # Live Codex sessions aren't backed by ~/.claude/sessions files; they're
    # discovered from running processes. Late import to avoid a circular
    # dependency (codex imports HOME_BASE from this module).
    try:
        from . import codex
        for w in codex.list_codex_windows():
            if w.pid == pid:
                return w
    except Exception:
        pass
    return None


def find_window_by_session(session_id: str) -> Optional[Window]:
    """Resolve a window by its Claude/Codex session id, or a unique prefix.

    This is the reverse lookup of `find_window`: humans and external tools
    (skills, monitors, scripts) usually hold a session id — e.g. from a
    transcript filename — not a pid. Prefixes must be >= 8 chars to avoid
    accidental matches; an ambiguous prefix resolves to nothing rather than
    to the wrong session.
    """
    sid = (session_id or "").strip().lower()
    if not sid:
        return None

    def _candidates():
        yield from list_windows(include_dead=True)
        # Process-discovered Claude sessions (no session file yet) — e.g. a
        # `claude --resume <id>` parked on the summary picker, whose id we want
        # resume/fork/locate to resolve. See find_window.
        yield from list_claude_proc_windows(set(), set())
        # Live Codex sessions come from process discovery (see find_window).
        try:
            from . import codex
            yield from codex.list_codex_windows()
        except Exception:
            pass

    prefix_matches: list[Window] = []
    for w in _candidates():
        wid = (w.session_id or "").lower()
        if wid == sid:
            return w
        if len(sid) >= 8 and wid.startswith(sid):
            prefix_matches.append(w)
    if len(prefix_matches) == 1:
        return prefix_matches[0]
    return None


def snapshot() -> dict:
    """Top-level state for the dashboard."""
    wins = list_windows()
    # Surface live `claude` processes that haven't registered a session file yet
    # (fresh spawns / resume parked on the summary picker) so they still card.
    known_pids = {w.pid for w in wins}
    known_ttys = {w.tty for w in wins if w.tty}
    known_sids = {w.session_id for w in wins if w.session_id}
    wins.extend(list_claude_proc_windows(known_pids, known_ttys, known_sids))
    # Counts cover only real user windows; `.slock` agent sub-sessions are
    # rendered separately at the bottom of the dashboard and excluded here.
    # These status-based tallies are a fallback: the app layer recomputes
    # `counts` by triage (after attaching it) so the header chips match.
    visible = [w for w in wins if not w.hidden]
    waiting = [w for w in visible if w.status == "waiting"]
    busy = [w for w in visible if w.status == "busy"]
    return {
        "windows": [w.to_dict() for w in wins],
        "counts": {
            "total": len(visible),
            "busy": len(busy),
            "waiting": len(waiting),
            "idle": len(visible) - len(busy) - len(waiting),
        },
        "ts": int(time.time() * 1000),
    }

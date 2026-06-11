"""Parse ~/.codex/sessions/ into HistorySession-compatible objects + timeline.

Also discovers *live* Codex TUI sessions from running processes so they can be
rendered as dashboard cards alongside Claude Code windows. Codex doesn't write a
pid-keyed session file the way Claude does, but a running interactive session
holds its `rollout-*.jsonl` transcript open as a file descriptor — so we map
process -> session via /proc/<pid>/fd (Linux only; degrades to nothing else).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .sessions import (
    HOME_BASE,
    Window,
    _cwd_to_project_slug,
    _pid_alive,
    get_tty,
)

CODEX_HOME = HOME_BASE / ".codex"
CODEX_SESSIONS_DIR = CODEX_HOME / "sessions"

# Idle thresholds for live-card triage, mirroring core.patrol.
_IDLE_THRESHOLD = 300       # 5 min — past this an idle session reads as "done"
_CLOSEABLE_THRESHOLD = 3600  # 1 hour — past this it's safe to suggest closing
# A rollout written within this window means the agent is actively producing
# output right now, regardless of what the last parsed event type was.
_BUSY_MTIME_WINDOW = 5.0

# Match skill path like /.claude/skills/foo/ or /.codex/skills/foo/
# Stop at whitespace, quote, &&, ||, semicolons, or maxdepth/-flag args
_SKILL_PATH_RE = re.compile(r'/\.(?:claude|codex)/skills/([A-Za-z0-9_-]+)(?:/|\b)')
_MEMORY_PATH_RE = re.compile(r'/memory/([A-Za-z0-9_-]+)\.md')

# Codex reuses the role=user message shape for synthetic, non-prompt turns it
# injects itself — each wrapped in a lowercase XML-ish tag (<environment_context>,
# <turn_aborted>, <subagent_notification>, <skill>, …). These are not the user's
# prompt and must be skipped when surfacing "what the user said".
_SYNTHETIC_USER_RE = re.compile(r'^<[a-z_]+>')


def _is_synthetic_user_text(text: str) -> bool:
    return bool(_SYNTHETIC_USER_RE.match(text.lstrip()))


# Codex's `/clear` wipes the TUI screen and conversation context, but it does
# NOT erase the rollout JSONL the card is rendered from — so the card would keep
# showing the pre-clear prompt/response. We record when each session (keyed by
# the stable card pid) was cleared and hide rollout events older than that, so
# the card blanks immediately and refills once a new prompt is sent. In-memory:
# a server restart forgets it, which at worst re-shows old preview text briefly.
_cleared_at_ms: dict[int, int] = {}


def mark_cleared(pid: int) -> None:
    """Record that the card for `pid` was cleared now; older rollout events hide."""
    _cleared_at_ms[pid] = int(time.time() * 1000)


def cleared_at_ms(pid: int) -> int:
    """Epoch-ms the card for `pid` was last cleared, or 0 if never."""
    return _cleared_at_ms.get(pid, 0)


def _before_clear(ts: str, since_ms: int) -> bool:
    """True if rollout-event timestamp `ts` predates the clear cutoff `since_ms`.

    Unparseable timestamps (returns 0) are never hidden — better to show a stray
    line than to blank a card we can't reason about.
    """
    if since_ms <= 0:
        return False
    t = _parse_iso_ms(ts)
    return 0 < t < since_ms


@dataclass
class CodexSession:
    session_id: str
    project: str
    project_name: str
    first_input: str
    first_ts: str
    last_ts: str
    transcript_path: str
    transcript_size: int
    transcript_mtime: int
    cli_version: str
    model_provider: str
    model: str = ""
    skills_used: list = field(default_factory=list)
    memory_ops: list = field(default_factory=list)
    skill_breakdown: dict = field(default_factory=dict)

    def to_history_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "project": self.project,
            "project_name": self.project_name,
            "first_input": self.first_input,
            "input_count": 0,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "transcript_path": self.transcript_path,
            "transcript_size": self.transcript_size,
            "transcript_mtime": self.transcript_mtime,
            "is_alive": False,
            "platform": "codex",
            "model": self.model,
            "skills_used": self.skills_used,
            "memory_ops": self.memory_ops,
            "skill_breakdown": self.skill_breakdown,
        }


def _parse_session_meta(path: Path) -> Optional[dict]:
    try:
        with path.open() as f:
            first_line = f.readline()
            d = json.loads(first_line)
            if d.get("type") != "session_meta":
                return None
            return d.get("payload") or {}
    except Exception:
        return None


def _extract_first_user_input(path: Path, since_ms: int = 0) -> str:
    """Return the user's first real prompt; fall back to the first assistant reply.

    Codex logs a submitted prompt two ways: a clean `event_msg`/`user_message`
    (the text typed into the TUI) and a `response_item` message with role=user
    carrying `input_text`. The role=user shape is *also* used for synthetic
    injections (`<environment_context>`, …), so those are skipped. If no user
    text is found at all, the first assistant `output_text` is returned.
    """
    fallback = ""
    try:
        with path.open() as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if _before_clear(d.get("timestamp", ""), since_ms):
                    continue
                t = d.get("type")
                payload = d.get("payload") or {}

                if t == "event_msg" and payload.get("type") == "user_message":
                    msg = (payload.get("message") or "").strip()
                    if msg:
                        return msg[:300]

                if t == "response_item" and payload.get("type") == "message":
                    role = payload.get("role")
                    for c in (payload.get("content") or []):
                        if not isinstance(c, dict):
                            continue
                        if role == "user" and c.get("type") == "input_text":
                            txt = (c.get("text") or "").strip()
                            if txt and not _is_synthetic_user_text(txt):
                                return txt[:300]
                        elif c.get("type") == "output_text" and not fallback:
                            txt = (c.get("text") or "").strip()
                            if txt:
                                fallback = txt[:300]
    except Exception:
        pass
    return fallback


def extract_codex_session_activity(path: Path | str) -> dict:
    """Codex has no file I/O tools — everything goes through exec_command.
    We must scan the command strings for skill/memory file references.
    """
    p = Path(path)
    if not p.exists():
        return {
            "skills_used": [], "memory_ops": [], "model": "",
            "skill_breakdown": {
                "per_skill_invokes": {}, "per_skill_reads": {},
                "per_skill_writes": {}, "per_skill_bash_refs": {},
            },
        }

    bash_refs: dict[str, int] = {}
    skill_reads: dict[str, int] = {}
    skill_writes: dict[str, int] = {}
    memory_ops_seen: set[tuple[str, str]] = set()
    memory_ops: list[dict] = []
    model = ""

    try:
        with p.open() as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = d.get("type", "")
                payload = d.get("payload") or {}

                if t == "turn_context":
                    m = payload.get("model", "")
                    if m:
                        model = m

                if t != "response_item":
                    continue
                if payload.get("type") != "function_call":
                    continue
                name = payload.get("name", "")
                if name != "exec_command":
                    continue

                args_str = payload.get("arguments", "")
                try:
                    args = json.loads(args_str) if isinstance(args_str, str) else args_str
                except Exception:
                    args = {}
                cmd = str(args.get("cmd", "") or args.get("command", ""))
                workdir = str(args.get("workdir", ""))
                # Codex sets workdir to skill dir, then runs cmd inside it.
                # Need to scan both for skill references.
                haystack = cmd + " " + workdir
                if not haystack.strip():
                    continue

                # Skill path mentions (in cmd OR workdir)
                skill_matches = set(_SKILL_PATH_RE.findall(haystack))
                if skill_matches:
                    write_kw = any(k in cmd for k in ("write_file", " > ", " >> ", "tee ", "echo ", "cat <<", "cp ", "mv ", "mkdir"))
                    for sk in skill_matches:
                        bash_refs[sk] = bash_refs.get(sk, 0) + 1
                        if write_kw:
                            skill_writes[sk] = skill_writes.get(sk, 0) + 1
                        else:
                            skill_reads[sk] = skill_reads.get(sk, 0) + 1

                # Memory path mentions
                mem_matches = _MEMORY_PATH_RE.findall(haystack)
                for mem_name in set(mem_matches):
                    if mem_name == "MEMORY":
                        continue
                    write_kw = any(k in cmd for k in (" > ", " >> ", "tee ", "echo ", "cat <<"))
                    op = "write" if write_kw else "read"
                    key = (mem_name, op)
                    if key not in memory_ops_seen:
                        memory_ops_seen.add(key)
                        memory_ops.append({"name": mem_name, "operation": op})
    except Exception:
        pass

    skills_used = list(set(list(skill_reads.keys()) + list(skill_writes.keys())))
    return {
        "skills_used": skills_used,
        "memory_ops": memory_ops,
        "model": model,
        "skill_breakdown": {
            "per_skill_invokes": {},
            "per_skill_reads": skill_reads,
            "per_skill_writes": skill_writes,
            "per_skill_bash_refs": bash_refs,
        },
    }


def list_codex_sessions() -> list[CodexSession]:
    if not CODEX_SESSIONS_DIR.exists():
        return []
    sessions: list[CodexSession] = []
    for f in CODEX_SESSIONS_DIR.rglob("*.jsonl"):
        meta = _parse_session_meta(f)
        if not meta:
            continue
        try:
            st = f.stat()
        except Exception:
            continue
        cwd = meta.get("cwd", "")
        activity = extract_codex_session_activity(f)
        sessions.append(CodexSession(
            session_id=meta.get("id", f.stem),
            project=cwd,
            project_name=cwd.rsplit("/", 1)[-1] if cwd else f.stem,
            first_input=_extract_first_user_input(f),
            first_ts=meta.get("timestamp", ""),
            last_ts=meta.get("timestamp", ""),
            transcript_path=str(f),
            transcript_size=st.st_size,
            transcript_mtime=int(st.st_mtime * 1000),
            cli_version=meta.get("cli_version", ""),
            model_provider=meta.get("model_provider", ""),
            model=activity["model"],
            skills_used=activity["skills_used"],
            memory_ops=activity["memory_ops"],
            skill_breakdown=activity["skill_breakdown"],
        ))
    sessions.sort(key=lambda s: s.transcript_mtime, reverse=True)
    return sessions


def codex_timeline(path: str | Path, limit: int = 60, since_ms: int = 0) -> list[dict]:
    """Parse Codex JSONL into TurnEvent-compatible dicts.

    `since_ms` (set after a card's Clear) drops events older than the clear, so
    the timeline reflects the cleared session rather than the untouched rollout.
    """
    p = Path(path)
    if not p.exists():
        return []
    events: list[dict] = []
    try:
        with p.open() as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = d.get("type")
                ts = d.get("timestamp", "")
                if _before_clear(ts, since_ms):
                    continue
                payload = d.get("payload") or {}

                if t == "event_msg":
                    # The user's typed prompt is logged as a `user_message`
                    # event with the text in `message` (role=user response_item
                    # turns are reserved for synthetic injections, handled below).
                    if payload.get("type") == "user_message":
                        text = (payload.get("message") or "").strip()
                        if text:
                            events.append({
                                "ts": ts, "kind": "user_text",
                                "text": text[:4000], "tool": None,
                                "role": "user", "extra": {},
                            })

                elif t == "response_item":
                    item_type = payload.get("type", "")
                    if item_type == "function_call":
                        events.append({
                            "ts": ts, "kind": "tool_use",
                            "text": "", "tool": payload.get("name", "function"),
                            "role": "assistant",
                            "extra": {"arguments": (payload.get("arguments") or "")[:200]},
                        })
                    elif item_type == "function_call_output":
                        events.append({
                            "ts": ts, "kind": "tool_result",
                            "text": (payload.get("output") or "")[:200],
                            "tool": None, "role": "user", "extra": {},
                        })
                    elif item_type == "message":
                        content = payload.get("content")
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "output_text":
                                    events.append({
                                        "ts": ts, "kind": "assistant_text",
                                        "text": (c.get("text") or "")[:4000],
                                        "tool": None, "role": "assistant", "extra": {},
                                    })
    except Exception:
        pass
    return events[-limit:]


# ---------- live session discovery (running codex TUIs as dashboard cards) ----------

def _parse_iso_ms(ts: str) -> int:
    """Best-effort ISO-8601 -> epoch ms; 0 on failure."""
    if not ts:
        return 0
    try:
        s = ts.strip().replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except Exception:
        return 0


def _read_tail_events(path: Path, max_lines: int = 60) -> list[dict]:
    """Parse the last `max_lines` JSONL records of a rollout (newest last)."""
    try:
        with path.open() as f:
            lines = f.readlines()
    except Exception:
        return []
    out: list[dict] = []
    for raw in lines[-max_lines:]:
        try:
            out.append(json.loads(raw))
        except Exception:
            continue
    return out


def _last_assistant_text(path: Path, since_ms: int = 0) -> str:
    """Most recent assistant output_text, used as the card's current-task hint."""
    for d in reversed(_read_tail_events(path, max_lines=120)):
        if d.get("type") != "response_item":
            continue
        if _before_clear(d.get("timestamp", ""), since_ms):
            continue
        payload = d.get("payload") or {}
        if payload.get("type") != "message":
            continue
        for c in (payload.get("content") or []):
            if isinstance(c, dict) and c.get("type") == "output_text":
                t = (c.get("text") or "").strip()
                if t:
                    return t.split("\n")[0][:120]
    return ""


def _infer_codex_status(path: Path, mtime: float) -> str:
    """busy | idle, inferred from the last substantive rollout event + mtime.

    Codex rollouts carry no explicit status field, so we read the tail and look
    at the last meaningful event, skipping `token_count` telemetry noise:
      - a trailing `function_call` (a tool was issued, output pending) → busy
      - a rollout touched within the last few seconds → busy (actively writing)
      - otherwise → idle
    """
    if (time.time() - mtime) < _BUSY_MTIME_WINDOW:
        return "busy"
    last_kind = ""
    for d in _read_tail_events(path, max_lines=60):
        t = d.get("type", "")
        payload = d.get("payload") or {}
        if t == "event_msg" and payload.get("type") == "token_count":
            continue  # telemetry; not a real activity signal
        if t == "response_item":
            it = payload.get("type", "")
            if it in ("function_call", "function_call_output", "message"):
                last_kind = it
        elif t == "event_msg":
            last_kind = "event_" + str(payload.get("role") or payload.get("type") or "")
    return "busy" if last_kind == "function_call" else "idle"


def _format_idle(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    h, m = seconds // 3600, (seconds % 3600) // 60
    return f"{h}h{m}m" if m else f"{h}h"


def _classify_codex(status: str, idle_seconds: int, current_task: str) -> dict:
    """Map a codex window to the dashboard's triage vocabulary."""
    if status == "busy":
        return {"triage": "working", "reason": "正在工作", "suggestion": ""}
    idle_str = _format_idle(idle_seconds)
    tail = f"。{current_task}" if current_task else ""
    if idle_seconds >= _CLOSEABLE_THRESHOLD:
        return {"triage": "closeable", "reason": f"空闲 {idle_str}{tail}", "suggestion": "可以关闭"}
    if idle_seconds >= _IDLE_THRESHOLD:
        return {"triage": "completed", "reason": f"已完成，空闲 {idle_str}{tail}", "suggestion": "建议 review"}
    return {"triage": "completed", "reason": f"空闲 {idle_str}{tail}", "suggestion": ""}


def _proc_table() -> dict[int, dict]:
    """{pid: {ppid, tty, args}} for every process, via one `ps` call."""
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid=,ppid=,tty=,args="],
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode("utf-8", "replace")
    except Exception:
        return {}
    table: dict[int, dict] = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        table[pid] = {"ppid": ppid, "tty": parts[2], "args": parts[3]}
    return table


def _rollout_fd(pid: int) -> Optional[str]:
    """The codex rollout JSONL this pid holds open, or None.

    A running interactive codex session keeps its transcript fd open; the
    background `mcp-server`/`app-server` codex processes do not, so this check
    naturally selects only real user-facing sessions.
    """
    fd_dir = f"/proc/{pid}/fd"
    sessions_marker = str(CODEX_SESSIONS_DIR)
    try:
        names = os.listdir(fd_dir)
    except Exception:
        return None
    for n in names:
        try:
            target = os.readlink(os.path.join(fd_dir, n))
        except Exception:
            continue
        if "rollout-" in target and target.endswith(".jsonl") and sessions_marker in target:
            return target
    return None


def _top_codex_ancestor(fd_pid: int, table: dict[int, dict]) -> int:
    """Walk up from the fd-holding inner process to the launcher process.

    The launcher (e.g. `node … codex --yolo`) is the right pid to expose as the
    card: killing it tears down the whole session, and it shares the tty with
    the inner binary so tmux-backed controls still resolve.
    """
    tty = table.get(fd_pid, {}).get("tty", "")
    cur = fd_pid
    seen = {cur}
    while True:
        pp = table.get(cur, {}).get("ppid", 0)
        info = table.get(pp)
        if not info or pp in seen:
            break
        if "codex" in info.get("args", "") and info.get("tty", "") == tty:
            cur = pp
            seen.add(cur)
        else:
            break
    return cur


# Codex subcommands that run headless/background, not an interactive TUI — these
# are spawned by editors or by Claude's codex MCP and shouldn't appear as cards.
_BG_SUBCOMMANDS = {"mcp-server", "app-server", "exec"}


def _codex_exe_index(tokens: list[str]) -> int:
    """Index of the `codex` executable token, or -1.

    Covers both the node launcher (`node …/bin/codex …`) and the inner binary
    (`…/bin/codex …`); the executable is among the first two tokens.
    """
    for i, t in enumerate(tokens[:2]):
        if os.path.basename(t) == "codex":
            return i
    return -1


def _is_interactive_codex(args: str) -> bool:
    """True for an interactive Codex TUI process (`codex`, `codex --yolo`,
    `codex resume …`); False for non-codex procs and background subcommands."""
    toks = args.split()
    i = _codex_exe_index(toks)
    if i < 0:
        return False
    for t in toks[i + 1:]:
        if t.startswith("-"):
            continue  # skip flags to reach the subcommand, if any
        return t not in _BG_SUBCOMMANDS
    return True  # bare `codex` with no subcommand → interactive TUI


def _proc_start_ms(pid: int) -> int:
    """Approximate process start time (ms) from the /proc/<pid> dir mtime."""
    try:
        return int(os.stat(f"/proc/{pid}").st_mtime * 1000)
    except Exception:
        return 0


def list_codex_windows() -> list[Window]:
    """Discover running interactive Codex sessions as Window objects.

    Detection is process-first (grouped by controlling tty) rather than purely
    fd-based: a freshly launched `codex` doesn't open its rollout transcript
    until the first turn, so a card must appear from the live process alone and
    get its session_id/transcript filled in once the rollout exists.

    Linux-only (reads /proc); returns [] on any platform without it.
    """
    if not Path("/proc").is_dir():
        return []
    table = _proc_table()
    if not table:
        return []

    # Group interactive codex processes (launcher + inner binary) by their tty;
    # one foreground tty == one session.
    by_tty: dict[str, list[int]] = {}
    for pid, info in table.items():
        tty = info.get("tty", "")
        if not tty or tty in ("?", "??"):
            continue
        if not _is_interactive_codex(info.get("args", "")):
            continue
        by_tty.setdefault(tty, []).append(pid)

    windows: list[Window] = []
    seen: set[int] = set()
    for tty, pids in by_tty.items():
        # The inner binary holds the rollout fd once a turn has happened.
        rollout = None
        fd_pid = None
        for pid in pids:
            rp = _rollout_fd(pid)
            if rp:
                rollout, fd_pid = rp, pid
                break
        anchor = fd_pid or min(pids)
        card_pid = _top_codex_ancestor(anchor, table)
        if card_pid in seen or not _pid_alive(card_pid):
            continue
        seen.add(card_pid)

        cwd = ""
        for pid in (anchor, card_pid, *pids):
            try:
                cwd = os.readlink(f"/proc/{pid}/cwd")
                break
            except Exception:
                continue

        if rollout:
            rp = Path(rollout)
            try:
                mtime = rp.stat().st_mtime
            except Exception:
                mtime = None
            meta = _parse_session_meta(rp) or {}
            cwd = cwd or meta.get("cwd", "") or ""
            status = _infer_codex_status(rp, mtime) if mtime else "idle"
            updated_at = int(mtime * 1000) if mtime else _proc_start_ms(card_pid)
            started_at = _parse_iso_ms(meta.get("timestamp", "")) or updated_at
            session_id = meta.get("id", rp.stem)
            version = str(meta.get("cli_version", ""))
            transcript = str(rp)
        else:
            # Just launched: no rollout yet. Show the card anyway, keyed by pid.
            start = _proc_start_ms(card_pid)
            status = "idle"
            updated_at = started_at = start
            session_id = f"codex-{card_pid}"
            version = ""
            transcript = None

        windows.append(Window(
            pid=card_pid,
            session_id=session_id,
            cwd=cwd,
            project_name=os.path.basename(cwd) or (cwd or session_id),
            project_slug=_cwd_to_project_slug(cwd),
            name=None,
            status=status,
            waiting_for=None,
            started_at=started_at,
            updated_at=updated_at,
            version=version,
            tty=get_tty(card_pid),
            transcript_path=transcript,
            alive=True,
            hidden=False,
            platform="codex",
        ))

    windows.sort(key=lambda w: (-w.updated_at, w.pid))
    return windows


def codex_window_dicts() -> list[dict]:
    """Live codex windows as fully-enriched dashboard dicts (skills/memory/
    triage/current_task), ready to merge into the snapshot alongside Claude
    windows. Shell-process counts are filled in by the caller (platform-agnostic).
    """
    out: list[dict] = []
    for w in list_codex_windows():
        d = w.to_dict()
        tp = Path(w.transcript_path) if w.transcript_path else None
        since = cleared_at_ms(w.pid)
        activity = extract_codex_session_activity(tp) if tp else {
            "skills_used": [], "memory_ops": [], "model": "",
        }
        current_task = _last_assistant_text(tp, since) if tp else ""
        tri = _classify_codex(w.status, d.get("idle_seconds", 0), current_task)
        d.update({
            "shell_proc_count": 0,            # caller overwrites via one ps walk
            "permission_msg": None,
            "permission_ts": None,
            "first_input": (_extract_first_user_input(tp, since) if tp else "")[:100],
            "current_task": current_task or None,
            "triage": tri["triage"],
            "triage_reason": tri["reason"],
            "triage_suggestion": tri["suggestion"],
            "skills_used": activity.get("skills_used", []),
            "memory_ops": activity.get("memory_ops", []),
            "background_tasks": [],
            "queued": [],
            "model": activity.get("model", ""),
        })
        out.append(d)
    return out

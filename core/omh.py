"""Live oh-my-humanize (omh) sessions as dashboard cards.

`omh` is a shell launcher that execs `bun --preload …/scripts/omp.ts
…/packages/coding-agent/src/cli.ts`, so one interactive session is one bun
process on a tty. Its transcript is a JSONL file under ~/.omp/agent/sessions/,
and omh itself records which file a terminal is on: the breadcrumb
~/.omp/agent/terminal-sessions/pts-N holds the cwd, then the session path, and
is rewritten whenever that terminal switches sessions (startup, /new, resume).
The card follows that pointer instead of guessing by mtime. The session file
itself appears only once the first message is written, so a fresh spawn shows
as an idle card with no transcript yet.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from .codex import (
    _BUSY_MTIME_WINDOW,
    _classify_codex,
    _proc_start_ms,
    _proc_table,
    _read_tail_events,
)
from .sessions import HOME_BASE, Window, _cwd_to_project_slug, _cwd_visible, _pid_alive, get_tty
from .textcap import MESSAGE_CHARS, TOOL_ARG_CHARS, TOOL_RESULT_CHARS, cap_text

BREADCRUMB_DIR = HOME_BASE / ".omp" / "agent" / "terminal-sessions"

_CLI_TAIL = "packages/coding-agent/src/cli.ts"
# Flags that make the CLI run headless and exit (print / rpc / export), which an
# omh agent's own bash tool may launch on a pty of its own — never a card.
_HEADLESS_FLAGS = {"-p", "--print", "--mode", "--export"}


def _is_interactive_omh(args: str) -> bool:
    """True for the bun process of an interactive omh TUI."""
    toks = args.split()
    for i, t in enumerate(toks):
        if os.path.normpath(t).endswith(_CLI_TAIL):
            return not any(a.split("=", 1)[0] in _HEADLESS_FLAGS for a in toks[i + 1:])
    return False


def _breadcrumb_session(tty: str) -> Optional[Path]:
    """The session file omh says `tty` (e.g. "pts/115") is on, or None."""
    try:
        lines = (BREADCRUMB_DIR / tty.replace("/", "-")).read_text().splitlines()
    except OSError:
        return None
    return Path(lines[1]) if len(lines) > 1 and lines[1] else None


def _messages(entries: list[dict]) -> list[tuple[str, dict]]:
    """(timestamp, message) for every `message` entry, in file order."""
    return [(d.get("timestamp", ""), d["message"]) for d in entries
            if d.get("type") == "message" and isinstance(d.get("message"), dict)]


def _text(content) -> str:
    """The text parts of a message's content (a string or a list of parts)."""
    if isinstance(content, str):
        return content
    return "\n".join(c.get("text") or "" for c in content or []
                     if isinstance(c, dict) and c.get("type") == "text")


def _first_user_input(path: Path) -> str:
    """The first prompt typed into the session — the card's title."""
    try:
        with path.open() as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                for _, m in _messages([d]):
                    if m.get("role") == "user" and not m.get("synthetic"):
                        t = _text(m.get("content")).strip()
                        if t:
                            return t
    except OSError:
        pass
    return ""


def _status(msgs: list[tuple[str, dict]], mtime: float) -> str:
    """busy | idle. A turn is under way while the last message is the user's,
    a tool result, or an assistant reply that stopped to call a tool; any other
    assistant stop (stop / error / aborted / length) ends the turn."""
    if time.time() - mtime < _BUSY_MTIME_WINDOW:
        return "busy"
    for _, m in reversed(msgs):
        role = m.get("role")
        if role == "assistant":
            return "busy" if m.get("stopReason") == "toolUse" else "idle"
        if role in ("user", "toolResult"):
            return "busy"
    return "idle"


def _last_assistant(msgs: list[tuple[str, dict]]) -> dict:
    return next((m for _, m in reversed(msgs) if m.get("role") == "assistant"), {})


def _current_task(msgs: list[tuple[str, dict]]) -> str:
    """First line of the latest assistant text, as the card's task hint."""
    for _, m in reversed(msgs):
        if m.get("role") == "assistant":
            t = _text(m.get("content")).strip()
            if t:
                return t.split("\n")[0][:120]
    return ""


def list_omh_windows() -> list[Window]:
    """Running interactive omh TUIs, one Window per tty, minus those the
    machine-local cwd filter hides. Linux-only (/proc)."""
    if not Path("/proc").is_dir():
        return []
    windows: list[Window] = []
    for pid, info in _proc_table().items():
        tty = info.get("tty", "")
        if not tty or tty in ("?", "??") or not _is_interactive_omh(info.get("args", "")):
            continue
        if not _pid_alive(pid):
            continue
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            cwd = ""
        if not _cwd_visible(cwd):
            continue
        started_at = _proc_start_ms(pid)
        path = _breadcrumb_session(tty)
        session_id = path.stem.split("_", 1)[-1] if path else f"omh-{pid}"
        status, updated_at, transcript = "idle", started_at, None
        if path and path.exists():
            mtime = path.stat().st_mtime
            status = _status(_messages(_read_tail_events(path, max_lines=200)), mtime)
            updated_at, transcript = int(mtime * 1000), str(path)
        windows.append(Window(
            pid=pid,
            session_id=session_id,
            cwd=cwd,
            project_name=os.path.basename(cwd) or session_id,
            project_slug=_cwd_to_project_slug(cwd),
            name=None,
            status=status,
            waiting_for=None,
            started_at=started_at,
            updated_at=updated_at,
            version="",
            tty=get_tty(pid),
            transcript_path=transcript,
            alive=True,
            hidden=False,
            platform="omh",
        ))
    windows.sort(key=lambda w: (-w.updated_at, w.pid))
    return windows


def omh_window_dicts() -> list[dict]:
    """Live omh windows as dashboard dicts, the shape codex_window_dicts gives.
    Shell-process counts are filled in by the caller."""
    out: list[dict] = []
    for w in list_omh_windows():
        d = w.to_dict()
        tp = Path(w.transcript_path) if w.transcript_path else None
        msgs = _messages(_read_tail_events(tp, max_lines=200)) if tp else []
        last = _last_assistant(msgs)
        current_task = _current_task(msgs)
        tri = _classify_codex(w.status, d.get("idle_seconds", 0), current_task)
        model = last.get("model", "")
        d.update({
            "shell_proc_count": 0,
            "permission_msg": None,
            "permission_ts": None,
            "first_input": (_first_user_input(tp) if tp else "")[:100],
            "current_task": current_task or None,
            "last_error": (str(last.get("errorMessage") or "")[:300] or None)
                          if last.get("stopReason") == "error" else None,
            "triage": tri["triage"],
            "triage_reason": tri["reason"],
            "triage_suggestion": tri["suggestion"],
            "skills_used": [],
            "memory_ops": [],
            "background_tasks": [],
            "queued": [],
            "model": model,
            "effort": "",
            "model_label": model,
            "model_source": "transcript" if model else "",
        })
        out.append(d)
    return out


def omh_timeline(path: str | Path, limit: int = 60) -> list[dict]:
    """An omh session JSONL as TurnEvent-compatible dicts (see codex_timeline)."""
    p = Path(path)
    entries: list[dict] = []
    try:
        with p.open() as f:
            for line in f:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
    except OSError:
        return []
    events: list[dict] = []

    def add(ts, kind, text="", tool=None, role="assistant", extra=None):
        events.append({"ts": ts, "kind": kind, "text": text, "tool": tool,
                       "role": role, "extra": extra or {}})

    for ts, m in _messages(entries):
        role = m.get("role")
        if role == "user" and not m.get("synthetic"):
            t = _text(m.get("content")).strip()
            if t:
                add(ts, "user_text", cap_text(t, MESSAGE_CHARS), role="user")
        elif role == "assistant":
            for c in m.get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text" and (c.get("text") or "").strip():
                    add(ts, "assistant_text", cap_text(c["text"], MESSAGE_CHARS))
                elif c.get("type") == "toolCall":
                    args = json.dumps(c.get("arguments") or {}, ensure_ascii=False)
                    add(ts, "tool_use", tool=c.get("name", "tool"),
                        extra={"arguments": cap_text(args, TOOL_ARG_CHARS)})
        elif role == "toolResult":
            add(ts, "tool_result", cap_text(_text(m.get("content")), TOOL_RESULT_CHARS),
                role="user")
    return events[-limit:]

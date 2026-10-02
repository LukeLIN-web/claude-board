"""Live humanize (`hmz`) TUIs as dashboard cards.

`hmz` with no command opens its terminal interface, which runs flows that drive
the claude / codex CLIs. Its composer is a ❯ prompt between two rules, the same
shape as Claude Code's, so the board types into it on Claude's send path.

What the card reports comes from the run, not the TUI. Every run of a flow is an
epic, ~/.humanize/epics/<workspace>/<when>-<which>/epic.jsonl, one event a line:
`began` (flow, task, the agent each role runs), `opened` (a session an agent
opened), `called` / `returned` (a flow it called), and `ended` (`how`: done,
failed or stopped). <workspace> is the cwd with every non-alphanumeric character
turned into "-", and the TUI reopens on the newest run of its directory — so the
card reads that one: begun and not ended means a flow is running.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

from .codex import _classify_codex, _proc_start_ms, _proc_table
from .sessions import HOME_BASE, Window, _cwd_to_project_slug, _cwd_visible, _pid_alive, get_tty
from .textcap import MESSAGE_CHARS, cap_text

EPICS_DIR = HOME_BASE / ".humanize" / "epics"

# Commands that are not the interface: `hmz exec` runs a flow headless, and
# `hmz internal …` is the sandbox / credential plumbing under every turn.
_HEADLESS = {"exec", "internal"}
_PLAIN = re.compile(r"[^A-Za-z0-9]")


def _is_interactive_hmz(args: str) -> bool:
    """True for an `hmz` process that is its terminal interface."""
    toks = args.split()
    for i, t in enumerate(toks[:3]):
        if os.path.basename(t) == "hmz":
            return not any(a in _HEADLESS for a in toks[i + 1:])
    return False


def _latest_epic(cwd: str) -> Optional[Path]:
    """epic.jsonl of the newest run in `cwd`, or None before the first one."""
    runs = EPICS_DIR / _PLAIN.sub("-", cwd)
    try:
        names = sorted(n for n in os.listdir(runs) if (runs / n / "epic.jsonl").is_file())
    except OSError:
        return None
    return runs / names[-1] / "epic.jsonl" if names else None


def _events(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        with path.open() as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if isinstance(d, dict):
                    out.append(d)
    except OSError:
        pass
    return out


def _began(events: list[dict]) -> dict:
    return next((e for e in events if e.get("event") == "began"), {})


def _ended(events: list[dict]) -> dict:
    return next((e for e in reversed(events) if e.get("event") == "ended"), {})


def _models(began: dict) -> str:
    """The distinct cli/model pairs the run's roles are on, e.g. "claude/claude-opus-5-5"."""
    seen: list[str] = []
    for a in began.get("agents") or []:
        if isinstance(a, dict) and a.get("model"):
            m = f"{a.get('backend')}/{a['model']}"
            if m not in seen:
                seen.append(m)
    return ", ".join(seen)


def _current_task(events: list[dict]) -> str:
    """`<flow> · <latest agent to open a session>`, or how the run ended."""
    flow = _began(events).get("flow", "")
    end = _ended(events)
    if end:
        return f"{flow} {end.get('how', 'ended')}"
    agent = next((e.get("agent") for e in reversed(events) if e.get("event") == "opened"), "")
    return f"{flow} · {agent}" if agent else flow


def list_hmz_windows() -> list[Window]:
    """Running hmz interfaces, one Window per tty, minus those the machine-local
    cwd filter hides. Linux-only (/proc)."""
    if not Path("/proc").is_dir():
        return []
    windows: list[Window] = []
    for pid, info in _proc_table().items():
        tty = info.get("tty", "")
        if not tty or tty in ("?", "??") or not _is_interactive_hmz(info.get("args", "")):
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
        epic = _latest_epic(cwd)
        status, updated_at, session_id = "idle", started_at, f"hmz-{pid}"
        if epic:
            events = _events(epic)
            status = "busy" if _began(events) and not _ended(events) else "idle"
            updated_at = max(started_at, int(epic.stat().st_mtime * 1000))
            session_id = epic.parent.name
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
            transcript_path=str(epic) if epic else None,
            alive=True,
            hidden=False,
            platform="hmz",
        ))
    windows.sort(key=lambda w: (-w.updated_at, w.pid))
    return windows


def hmz_window_dicts() -> list[dict]:
    """Live hmz windows as dashboard dicts, the shape codex_window_dicts gives.
    Shell-process counts are filled in by the caller."""
    out: list[dict] = []
    for w in list_hmz_windows():
        d = w.to_dict()
        events = _events(Path(w.transcript_path)) if w.transcript_path else []
        began, end = _began(events), _ended(events)
        current_task = _current_task(events)
        tri = _classify_codex(w.status, d.get("idle_seconds", 0), current_task)
        models = _models(began)
        d.update({
            "shell_proc_count": 0,
            "permission_msg": None,
            "permission_ts": None,
            "first_input": str(began.get("task") or "").strip().split("\n")[0][:100],
            "current_task": current_task or None,
            "last_error": f"{began.get('flow', 'run')} failed" if end.get("how") == "failed" else None,
            "triage": tri["triage"],
            "triage_reason": tri["reason"],
            "triage_suggestion": tri["suggestion"],
            "skills_used": [],
            "memory_ops": [],
            "background_tasks": [],
            "queued": [],
            "model": models,
            "effort": "",
            "model_label": models,
            "model_source": "transcript" if models else "",
        })
        out.append(d)
    return out


def hmz_timeline(path: str | Path, limit: int = 60) -> list[dict]:
    """A run's epic.jsonl as TurnEvent-compatible dicts: the task it began on,
    each session an agent opened, each flow it called, and how it ended. What
    the sessions said lives in their own CLI's transcripts, not here."""
    events: list[dict] = []
    for e in _events(Path(path)):
        kind, ts = e.get("event"), e.get("at", "")
        if kind == "began":
            text = f"${e.get('flow', '')} {e.get('task', '')}".strip()
            events.append({"ts": ts, "kind": "user_text", "text": cap_text(text, MESSAGE_CHARS),
                           "tool": None, "role": "user", "extra": {}})
            continue
        if kind == "opened":
            text = f"{e.get('agent', '')} opened a {e.get('backend', '')} session"
        elif kind == "called":
            text = f"called flow {e.get('flow', '')}"
        elif kind == "returned":
            text = f"flow {e.get('flow', '')} returned"
        elif kind == "ended":
            text = f"run ended: {e.get('how', '')}"
        else:
            continue
        events.append({"ts": ts, "kind": "assistant_text", "text": text,
                       "tool": None, "role": "assistant", "extra": {}})
    return events[-limit:]

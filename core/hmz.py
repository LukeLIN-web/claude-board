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

~/.humanize is $HUMANIZE_HOME when the hmz was started with one, as hmz's own
`home()` has it.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Callable, Optional

from . import tmux
from .codex import _classify_codex, _proc_start_ms, _proc_table
from .sessions import HOME_BASE, Window, _cwd_to_project_slug, _cwd_visible, _pid_alive, get_tty
from .textcap import MESSAGE_CHARS, cap_text

HMZ_HOME = HOME_BASE / ".humanize"

# What the timeline says for an hmz that hasn't run anything yet: there is no
# epic to read until the first line is submitted in it.
NO_RUN_NOTE = ("这个 hmz 还没开始 run，没有可显示的内容。在它的输入框里提交一行后才会有："
               "普通的一行交给当前 flow（状态栏上 ◉ 后面那个），`$<flow> <任务>` 启动指定的 flow。")

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


def _home(pid: int) -> Path:
    """Where hmz `pid` keeps its runs and its history."""
    try:
        env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        env = []
    for kv in env:
        if kv.startswith(b"HUMANIZE_HOME="):
            v = kv.split(b"=", 1)[1].decode(errors="replace")
            if v:
                return Path(v)
    return HMZ_HOME


def _latest_epic(cwd: str, home: Optional[Path] = None) -> Optional[Path]:
    """epic.jsonl of the newest run in `cwd`, or None before the first one."""
    runs = (home or HMZ_HOME) / "epics" / _PLAIN.sub("-", cwd)
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
        epic = _latest_epic(cwd, _home(pid))
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


def _said(path: Path, start: int = 0) -> list[tuple[str, str]]:
    """(workdir, text) of each line in hmz's history.jsonl from byte `start` on."""
    out: list[tuple[str, str]] = []
    try:
        with path.open("rb") as f:
            f.seek(start)
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return out
    for line in data.splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        if isinstance(d, dict) and isinstance(d.get("text"), str):
            out.append((str(d.get("workdir") or ""), d["text"]))
    return out


def _squeeze(s: str) -> str:
    return "".join(s.split())


def prompt_taken(pid: int, cwd: str, text: str, pane: str) -> Callable[[], bool]:
    """A check that hmz `pid` took `text`, set up before it is pasted.

    An emptied composer proves nothing with hmz (see tmux.send_text_confirmed).
    What does: hmz writes every line it takes — a task, a word put into a
    running flow, a command — to <home>/history.jsonl before acting on it. It
    skips the line it was last given, though, so a repeat of that falls back to
    the screen: the line echoed above an emptied composer.
    """
    path = _home(pid) / "history.jsonl"
    try:
        mark = path.stat().st_size
    except OSError:
        mark = 0
    want = _squeeze(text)
    said = _said(path)
    # hmz's "last given" is the newest line typed in this directory, or the
    # newest anywhere when nothing was ever typed here.
    here = [t for where, t in said if where == cwd] or [t for _, t in said]
    if here and _squeeze(here[-1]) == want:
        return lambda: tmux._shown_above_composer(pane, text)
    return lambda: any(_squeeze(t) == want for _, t in _said(path, mark))


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

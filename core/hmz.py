"""Live humanize (`hmz`) TUIs as dashboard cards.

`hmz` with no command opens its terminal interface, which runs flows that drive
the claude / codex CLIs. Its composer is a ❯ prompt between two rules, the same
shape as Claude Code's, so the board types into it on Claude's send path.

What the card reports comes from the run, not the TUI. Every run of a flow is an
epic, ~/.hmz/epics/<workspace>/<when>-<which>/epic.jsonl, one event a line:
`began` (flow, task, the agent each role runs), `opened` (a session an agent
opened), `called` / `returned` (a flow it called), and `ended` (`how`: done,
failed or stopped). <workspace> is the cwd with every non-alphanumeric character
turned into "-", and the TUI reopens on the newest run of its directory — so the
card reads that one: begun and not ended means a flow is running.

What the agents said is not in the epic but beside it. A flow another flow
called writes its own record, epic.<flow>_<id>.jsonl in the same directory, and
the sessions opened inside it are written down there. Each session an agent
opens is kept in the run too, under <epic>/sessions/<cli>/ laid out as that CLI
lays out its home — projects/<dir>/<id>.jsonl for Claude, sessions/<y>/<m>/<d>/
rollout-…-<id>.jsonl for Codex — and its `opened` line says where (`where`,
relative to the epic, or whole for a session that stayed in the CLI's own
home). Not in ~/.claude or ~/.codex, so no card of its own: the hmz card is the
only place those sessions show.

~/.hmz is $HUMANIZE_HOME when the hmz was started with one, as hmz's own
`home()` has it. It was ~/.humanize until hmz renamed it: a newer hmz moves the
old one over the first time it runs, and an older one goes on using it.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Callable, Optional

from . import codex, tmux, transcripts
from .codex import _classify_codex, _proc_start_ms, _proc_table
from .sessions import HOME_BASE, Window, _cwd_to_project_slug, _cwd_visible, _pid_alive, get_tty
from .textcap import MESSAGE_CHARS, cap_text

HMZ_HOME = HOME_BASE / ".hmz"
HMZ_HOME_WAS = HOME_BASE / ".humanize"

# What the timeline says for an hmz that hasn't run anything yet: there is no
# epic to read until the first line is submitted in it.
NO_RUN_NOTE = ("这个 hmz 还没开始 run，没有可显示的内容。在它的输入框里提交一行后才会有："
               "普通的一行交给当前 flow（状态栏上 ◉ 后面那个），`$<flow> <任务>` 启动指定的 flow。")
# …and for one that was typed into but started nothing: hmz wrote the lines down,
# then answered each on its own screen only.
TYPED_NO_RUN_NOTE = ("这个 hmz 还没开始 run：下面是输入给它的行，它收下了，但没有一行启动 flow。"
                     "它为什么不跑只写在它自己的屏幕上（比如 `hmz: no such flow: …`）。")

# Commands that are not the interface: `hmz exec` runs a flow headless, and
# `hmz internal …` is the sandbox / credential plumbing under every turn.
_HEADLESS = {"exec", "internal"}
_PLAIN = re.compile(r"[^A-Za-z0-9]")

# Where a CLI logs one session under the directory hmz keeps it in, as hmz's own
# backend profiles have it (hmz.coganchor.backends, `logs=`). Claude's subagent
# logs are left out: the card follows the agents the flow drove.
_LOGS = {
    "claude": "projects/*/{}.jsonl",
    "codex": "sessions/**/rollout-*{}.jsonl",
}


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
    return _default_home()


def _default_home() -> Path:
    """~/.hmz once there is one — hmz uses it, and leaves the old one alone,
    from then on — else ~/.humanize, which an hmz from before the rename keeps."""
    return HMZ_HOME if HMZ_HOME.exists() or not HMZ_HOME_WAS.exists() else HMZ_HOME_WAS


def _latest_epic(cwd: str, home: Optional[Path] = None) -> Optional[Path]:
    """epic.jsonl of the newest run in `cwd`, or None before the first one."""
    runs = (home or _default_home()) / "epics" / _PLAIN.sub("-", cwd)
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


def _records(epic: Path) -> list[Path]:
    """The run's own record, then one per flow it called: the sessions opened
    inside a called flow are written down in that flow's record."""
    try:
        called = sorted(p for p in epic.parent.glob("epic.*.jsonl") if p.is_file())
    except OSError:
        called = []
    return [epic] + called


def _opened(epic: Path) -> list[dict]:
    """Every `opened` line of the run, across all its records, oldest first."""
    lines = [e for r in _records(epic) for e in _events(r)
             if e.get("event") == "opened" and e.get("session")]
    return sorted(lines, key=lambda e: str(e.get("at", "")))


def _logs(epic: Path, opened: dict) -> list[Path]:
    """The log files of the session an `opened` line names, or [] for a CLI the
    board can't read or a log that has gone."""
    ident = str(opened["session"])
    pattern = _LOGS.get(str(opened.get("backend") or ""))
    if not pattern:
        return []
    where = str(opened.get("where") or "")
    if where:
        # Relative to the epic for a session kept in the run, whole for one that
        # stayed in its CLI's home — and `/` keeps a whole path whole.
        at, pattern = epic.parent / where, pattern.format(ident)
    else:
        # A run from before sessions were kept: a directory of links per session.
        at, pattern = epic.parent / "sessions" / str(opened.get("name") or ""), f"*{ident}*.jsonl"
    try:
        return sorted(p for p in at.glob(pattern) if p.is_file())
    except (OSError, ValueError):
        return []


def _last_logged(epic: Path) -> int:
    """Epoch ms of the newest write to any of the run's records or session logs.
    The epic itself is written only when a session opens or the run ends, so a
    turn hours long leaves it untouched."""
    paths = _records(epic) + [p for o in _opened(epic) for p in _logs(epic, o)]
    newest = 0.0
    for p in paths:
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            pass
    return int(newest * 1000)


def _models(began: dict) -> str:
    """The distinct cli/model pairs the run's roles are on, e.g. "claude/claude-opus-5-5"."""
    seen: list[str] = []
    for a in began.get("agents") or []:
        if isinstance(a, dict) and a.get("model"):
            m = f"{a.get('backend')}/{a['model']}"
            if m not in seen:
                seen.append(m)
    return ", ".join(seen)


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _current_task(events: list[dict], epic: Optional[Path] = None) -> str:
    """`<flow> · <agent at work>: <what its session is doing>`, or how the run
    ended. The agent at work is the one whose session log was written last —
    agents that take turns resume their sessions rather than open new ones —
    or, with no log to go by, the last to open one."""
    flow = _began(events).get("flow", "")
    end = _ended(events)
    if end:
        return f"{flow} {end.get('how', 'ended')}"
    opened = _opened(epic) if epic else [e for e in events if e.get("event") == "opened"]
    if not opened:
        return flow
    at_work, log, newest = opened[-1], None, -1.0
    for o in opened if epic else []:
        for p in _logs(epic, o):
            if _mtime(p) >= newest:
                at_work, log, newest = o, p, _mtime(p)
    task = f"{flow} · {at_work.get('agent', '')}"
    hint = ""
    if log and at_work.get("backend") == "claude":
        hint = transcripts.current_task_hint(log) or ""
    elif log and at_work.get("backend") == "codex":
        hint = codex._last_assistant_text(log)
    return f"{task}: {hint}" if hint else task


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
            updated_at = max(started_at, _last_logged(epic))
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
        epic = Path(w.transcript_path) if w.transcript_path else None
        events = _events(epic) if epic else []
        began, end = _began(events), _ended(events)
        current_task = _current_task(events, epic)
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


def _history(path: Path, start: int = 0) -> list[dict]:
    """The lines in hmz's history.jsonl from byte `start` on, each {at, workdir, text}."""
    out: list[dict] = []
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
            out.append({"at": str(d.get("at") or ""), "workdir": str(d.get("workdir") or ""),
                        "text": d["text"]})
    return out


def _said(path: Path, start: int = 0) -> list[tuple[str, str]]:
    """(workdir, text) of each line in hmz's history.jsonl from byte `start` on."""
    return [(d["workdir"], d["text"]) for d in _history(path, start)]


def typed(pid: int, cwd: str, since_ms: int) -> list[dict]:
    """The lines typed into hmz `pid` since it started, oldest first, as hmz
    wrote them down — whether or not any of them started anything."""
    return [d for d in _history(_home(pid) / "history.jsonl")
            if d["workdir"] == cwd and transcripts._parse_ts(d["at"]) * 1000 >= since_ms]


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


# hmz's own lines are the interface's, not an agent's, and each begins "hmz: ".
_REFUSAL = "hmz: "
_RULE = re.compile(r"^[─━\s]+$")


def refusal(pane: str, text: str) -> str:
    """What hmz said instead of acting on `text` — `hmz: no such flow: x` — or "".

    hmz writes a line down before reading it, so its history takes a line it then
    refuses: a `$flow` it doesn't have, a `/command` it doesn't know, a flow
    chosen while one runs. The refusal is only on its screen, the red line it
    puts right under the echo of what was typed (both above the composer).
    """
    lines = tmux.capture_pane(pane).get("text", "").splitlines()
    composer = next((i for i in range(len(lines) - 1, -1, -1)
                     if lines[i].lstrip().startswith("❯")), -1)
    needle = _squeeze(text)[-24:]
    if composer < 0 or not needle:
        return ""
    # Where the echo ends, found on the screen with its wrapping squeezed out.
    above = "\n".join(lines[:composer])
    at = [i for i, ch in enumerate(above) if not ch.isspace()]
    k = "".join(above[i] for i in at).rfind(needle)
    if k < 0:
        return ""
    said: list[str] = []
    for line in above[at[k + len(needle) - 1] + 1:].split("\n")[1:]:
        if not line.strip() or _RULE.match(line):
            if said:
                break
            continue
        if not said and not line.lstrip().startswith(_REFUSAL):
            return ""
        said.append(line.strip())
        if len(said) == 3:  # a long one wraps; the composer's own chrome follows it
            break
    return " ".join(said)


def _session_timeline(log: Path, backend: str, limit: int) -> list[dict]:
    if backend == "codex":
        return codex.codex_timeline(log, limit=limit)
    return transcripts.timeline(log, limit=limit)


def hmz_timeline(path: str | Path | None, limit: int = 60,
                 typed: list[dict] = ()) -> list[dict]:
    """A run as TurnEvent-compatible dicts: the task it began on, every turn of
    every session its agents opened — read from where the run keeps them, each
    tagged `extra.agent` with whose it was — each flow it called, and how it
    ended, in the order they happened.

    `typed` (see typed()) are the lines typed into the hmz; each one the run
    doesn't already show goes in where it was typed. A line hmz refused, or
    one typed before there was any run (`path` None), shows nowhere else."""
    epic = Path(path) if path else None
    events: list[dict] = []
    task = ""
    for e in _events(epic) if epic else []:
        kind, ts = e.get("event"), e.get("at", "")
        if kind == "began":
            task = _squeeze(cap_text(str(e.get("task") or ""), MESSAGE_CHARS))
            text = f"${e.get('flow', '')} {e.get('task', '')}".strip()
            events.append({"ts": ts, "kind": "user_text", "text": cap_text(text, MESSAGE_CHARS),
                           "tool": None, "role": "user", "extra": {}})
            continue
        if kind == "called":
            text = f"called flow {e.get('flow', '')}"
        elif kind == "returned":
            text = f"flow {e.get('flow', '')} returned"
        elif kind == "ended":
            text = f"run ended: {e.get('how', '')}"
        else:
            continue
        events.append({"ts": ts, "kind": "assistant_text", "text": text,
                       "tool": None, "role": "assistant", "extra": {}})
    # A forked session's log opens on a copy of the conversation it was cut
    # from, so a turn two sessions both hold is shown once.
    seen: set[tuple] = set()
    for o in _opened(epic) if epic else []:
        agent, backend = str(o.get("agent") or ""), str(o.get("backend") or "")
        said: list[dict] = []
        for log in _logs(epic, o):
            said += _session_timeline(log, backend, limit)
        if not said:
            # A CLI the board can't read, or a log that has gone: say the session
            # was opened, which is all the run itself knows.
            events.append({"ts": o.get("at", ""), "kind": "assistant_text",
                           "text": f"{agent} opened a {backend} session",
                           "tool": None, "role": "assistant", "extra": {"agent": agent}})
            continue
        for ev in said:
            key = (ev.get("ts"), ev.get("kind"), ev.get("tool"), ev.get("text"))
            if key in seen:
                continue
            seen.add(key)
            # The task the run began on, handed to its first agent word for
            # word, is already the run's own first line.
            if ev.get("kind") == "user_text" and task and _squeeze(ev.get("text") or "") == task:
                task = ""
                continue
            ev["extra"] = {**(ev.get("extra") or {}), "agent": agent}
            events.append(ev)
    # A typed line the run took is already here, as the task it began on or as
    # what an agent was told; hmz may wrap the latter, so it is looked for inside.
    shown = [_squeeze(ev.get("text") or "") for ev in events if ev.get("kind") == "user_text"]
    for d in typed:
        line = _squeeze(cap_text(d["text"], MESSAGE_CHARS))
        if line and not any(line in s for s in shown):
            events.append({"ts": d["at"], "kind": "user_text", "text": cap_text(d["text"], MESSAGE_CHARS),
                           "tool": None, "role": "user", "extra": {}})
    # Stable: the run's own lines keep their place among turns of the same instant.
    events.sort(key=lambda ev: transcripts._parse_ts(ev.get("ts") or ""))
    return events[-limit:]

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
import time
from pathlib import Path
from typing import Iterator, Optional

from . import patrol, transcripts
from .textcap import MESSAGE_CHARS, TOOL_ARG_CHARS, TOOL_RESULT_CHARS, cap_text
from .sessions import (
    HOME_BASE,
    Proc,
    Window,
    _cli_words,
    _cwd_to_project_slug,
    _cwd_visible,
    _exe_index,
    _pid_alive,
    _proc_argv,
    _proc_cwd,
    _proc_start_ms,
    _ps_tty,
    proc_table,
    transcript_visible,
)

CODEX_HOME = HOME_BASE / ".codex"
CODEX_SESSIONS_DIR = CODEX_HOME / "sessions"

# A rollout written within this window means the agent is actively producing
# output right now, regardless of what the last parsed event type was.
_BUSY_MTIME_WINDOW = 5.0

# Match skill path like /.claude/skills/foo/ or /.codex/skills/foo/
# Stop at whitespace, quote, &&, ||, semicolons, or maxdepth/-flag args
_SKILL_PATH_RE = re.compile(r'/\.(?:claude|codex)/skills/([A-Za-z0-9_-]+)(?:/|\b)')
_MEMORY_PATH_RE = re.compile(r'/memory/([A-Za-z0-9_-]+)\.md')

# Codex reuses the role=user message shape for synthetic, non-prompt turns it
# injects itself — each wrapped in a lowercase XML-ish tag (<environment_context>,
# <turn_aborted>, <subagent_notification>, <skill>, <codex_internal_context
# source="goal">, …). These are not the user's prompt and must be skipped when
# surfacing "what the user said"; the tag can carry attributes.
_SYNTHETIC_USER_RE = re.compile(r'^<[a-z_]+[\s>]')


def _is_synthetic_user_text(text: str) -> bool:
    return bool(_SYNTHETIC_USER_RE.match(text.lstrip()))


def _typed_user_parts(payload: dict) -> list[str]:
    """The typed parts of a role=user message — [] when the message is injected.

    Codex opens a session with one role=user message whose parts are the
    project's AGENTS.md and `<environment_context>`. Only the second part is
    tag-wrapped, so checking parts independently lets the AGENTS.md half through
    as if a person had typed it. A message with any injected part is injected.
    """
    parts: list[str] = []
    for c in (payload.get("content") or []):
        if not isinstance(c, dict) or c.get("type") != "input_text":
            continue
        txt = (c.get("text") or "").strip()
        if not txt:
            continue
        if _is_synthetic_user_text(txt):
            return []
        parts.append(txt)
    return parts


def _typed_item_text(payload: dict) -> str:
    """An `item_completed` event's text if it is the user's prompt, else "".

    Newer Codex builds write each turn as thread items — `item_completed` events
    carrying an `item` of type UserMessage / AgentMessage / CommandExecution / …
    — and stop emitting the flat `user_message` event entirely (seen from CLI
    0.147). The UserMessage item is what the person typed, already free of the
    injections mixed into the role=user records.
    """
    item = payload.get("item") or {}
    if item.get("type") != "UserMessage":
        return ""
    parts = [(c.get("text") or "").strip()
             for c in (item.get("content") or []) if isinstance(c, dict)]
    return "\n".join(p for p in parts if p).strip()


def _event_user_text(payload: dict) -> str:
    """The prompt an `event_msg` payload carries, "" when it carries none: the
    text of a `user_message`, or a newer build's UserMessage item (see
    _typed_item_text)."""
    if payload.get("type") == "user_message":
        return (payload.get("message") or "").strip()
    if payload.get("type") == "item_completed":
        return _typed_item_text(payload)
    return ""


def _tool_output_text(output) -> str:
    """A tool result's text, whether Codex wrote it as a string or as parts."""
    if isinstance(output, list):
        return "\n".join((c.get("text") or "")
                         for c in output if isinstance(c, dict)).strip()
    if isinstance(output, dict):
        return str(output.get("output") or output.get("text") or "")
    return str(output or "")


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
    t = transcripts._parse_ts(ts) * 1000
    return 0 < t < since_ms


def _records(path: Path) -> Iterator[dict]:
    """A rollout's lines, parsed; one that isn't a JSON object is skipped."""
    return (d for d in transcripts._iter_lines(path) if isinstance(d, dict))


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


# Asked for on every 2s tick per live card; after a Clear it reads every row from
# before the clear, until a new prompt is typed.
@transcripts.memo_by_file
def _extract_first_user_input(path: Path, since_ms: int = 0) -> str:
    """Return the user's first real prompt; fall back to the first assistant reply.

    Codex logs a submitted prompt three ways: a clean `event_msg`/`user_message`
    (the text typed into the TUI), the newer `event_msg`/`item_completed` with a
    UserMessage item, and a `response_item` message with role=user carrying
    `input_text`. The role=user shape is *also* used for synthetic injections
    (`<environment_context>`, the AGENTS.md preamble, …), so those are skipped.
    If no user text is found at all, the first assistant `output_text` is used.
    """
    fallback = ""
    try:
        for d in _records(path):
            if _before_clear(d.get("timestamp", ""), since_ms):
                continue
            t = d.get("type")
            payload = d.get("payload") or {}

            if t == "event_msg":
                msg = _event_user_text(payload)
                if msg:
                    return msg[:300]

            if t == "response_item" and payload.get("type") == "message":
                if payload.get("role") == "user":
                    typed = _typed_user_parts(payload)
                    if typed:
                        return "\n".join(typed)[:300]
                    continue
                for c in (payload.get("content") or []):
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "output_text" and not fallback:
                        txt = (c.get("text") or "").strip()
                        if txt:
                            fallback = txt[:300]
    except Exception:
        pass
    return fallback


# Parsed rollouts, kept while the file is unchanged — path → ((st_mtime_ns,
# st_size), result). The history index (rebuilt every 30s) lists every rollout:
# re-reading ~1k of them (~900 MB) for it took seconds each time. It cycles
# through more rollouts than memo_by_file keeps, so it keeps its own, pruned of
# the ones that are gone.
_session_cache: dict[Path, tuple[tuple[int, int], Optional[dict]]] = {}


def _clear_caches() -> None:
    """Forget every parsed rollout (tests / explicit refresh)."""
    _session_cache.clear()


# A live card asks for its activity every 2s tick.
@transcripts.memo_by_file
def extract_codex_session_activity(path: Path | str) -> dict:
    """Codex has no file I/O tools — everything goes through exec_command.
    We must scan the command strings for skill/memory file references.
    Read again only once the rollout changes; the result is shared, so callers
    must not change it.
    """
    return _scan_activity(Path(path))


def _scan_activity(p: Path) -> dict:
    bash_refs: dict[str, int] = {}
    skill_reads: dict[str, int] = {}
    skill_writes: dict[str, int] = {}
    memory_reads: dict[str, int] = {}
    memory_writes: dict[str, int] = {}
    memory_ops_seen: set[tuple[str, str]] = set()
    memory_ops: list[dict] = []
    model = ""
    effort = ""

    try:
        for d in _records(p):
            t = d.get("type", "")
            payload = d.get("payload") or {}

            if t == "turn_context":
                m = payload.get("model", "")
                if m:
                    model = m
                e = payload.get("effort", "")
                if e:
                    effort = e

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

            # Memory path mentions. A command can't say edit from write, so an
            # edit counts as a write.
            write_kw = any(k in cmd for k in (" > ", " >> ", "tee ", "echo ", "cat <<"))
            op, counts = ("write", memory_writes) if write_kw else ("read", memory_reads)
            for mem_name in set(_MEMORY_PATH_RE.findall(haystack)):
                if mem_name == "MEMORY":
                    continue
                counts[mem_name] = counts.get(mem_name, 0) + 1
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
        "effort": effort,
        "skill_breakdown": {
            "per_skill_invokes": {},
            "per_skill_reads": skill_reads,
            "per_skill_writes": skill_writes,
            "per_skill_bash_refs": bash_refs,
        },
        "memory_breakdown": {
            "per_memory_reads": memory_reads,
            "per_memory_writes": memory_writes,
            "per_memory_edits": {},
        },
    }


def list_codex_sessions() -> list[dict]:
    """Every rollout under ~/.codex/sessions as history.HistorySession fields,
    newest first. Each is parsed once per change (see _session_cache); the dicts
    are shared, so callers must not change them."""
    if not CODEX_SESSIONS_DIR.exists():
        return []
    sessions: list[dict] = []
    seen: set[Path] = set()
    for f in CODEX_SESSIONS_DIR.rglob("*.jsonl"):
        try:
            st = f.stat()
        except Exception:
            continue
        seen.add(f)
        key = (st.st_mtime_ns, st.st_size)
        hit = _session_cache.get(f)
        if hit is None or hit[0] != key:
            hit = _session_cache[f] = (key, _codex_session(f, st))
        if hit[1]:
            sessions.append(hit[1])
    # list() first: history requests can run this on two threads at once.
    for gone in [f for f in list(_session_cache) if f not in seen]:
        _session_cache.pop(gone, None)
    sessions.sort(key=lambda s: s["transcript_mtime"], reverse=True)
    return sessions


def _codex_session(f: Path, st: os.stat_result) -> Optional[dict]:
    """The rollout `f` (as stat'd in `st`) as history.HistorySession fields; None
    when its first line isn't session_meta."""
    meta = _parse_session_meta(f)
    if not meta:
        return None
    cwd = meta.get("cwd", "")
    activity = _scan_activity(f)
    return {
        "session_id": meta.get("id", f.stem),
        "project": cwd,
        "project_name": cwd.rsplit("/", 1)[-1] if cwd else f.stem,
        "first_input": _extract_first_user_input(f),
        "input_count": 0,
        "first_ts": meta.get("timestamp", ""),
        "last_ts": meta.get("timestamp", ""),
        "transcript_path": str(f),
        "transcript_size": st.st_size,
        "transcript_mtime": int(st.st_mtime * 1000),
        "is_alive": False,
        "platform": "codex",
        "model": activity["model"],
        "skills_used": activity["skills_used"],
        "memory_ops": activity["memory_ops"],
        "skill_breakdown": activity["skill_breakdown"],
        "memory_breakdown": activity["memory_breakdown"],
    }


def find_rollout(session_id: str) -> Optional[Path]:
    """The rollout an archived session's id names, or None.

    The id is one of two things: a search hit's is the rollout's file stem, and
    a history row's the id its session_meta records (see _codex_session). Both
    are matched whole — a substring match handed any short id, "2026" say, an
    arbitrary rollout. The file is named `rollout-<time>-<id>.jsonl`, and a
    thread kept as paginated history (`history_mode: paginated` in its meta)
    goes on in further pages `rollout-<time>-<id>_<page>.jsonl`, each opening
    with that same session_meta: the newest of them is where it stands now.
    """
    if not session_id or not CODEX_SESSIONS_DIR.exists():
        return None
    best: Optional[Path] = None
    best_mtime = -1.0
    for f in CODEX_SESSIONS_DIR.rglob("*.jsonl"):
        if f.stem == session_id:
            return f
        # The name says which files could be its pages; the meta settles it.
        if not (f.stem.endswith("-" + session_id) or f"-{session_id}_" in f.stem):
            continue
        if (_parse_session_meta(f) or {}).get("id") != session_id:
            continue
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if mtime > best_mtime:
            best, best_mtime = f, mtime
    return best


# An open card's timeline is asked for every 2.5 s, and without `tail` this reads
# the whole rollout.
@transcripts.memo_by_file
def codex_timeline(path: str | Path, limit: int = 60, since_ms: int = 0,
                   tail: int = 0) -> list[dict]:
    """Parse Codex JSONL into TurnEvent-compatible dicts.

    `since_ms` (set after a card's Clear) drops events older than the clear, so
    the timeline reflects the cleared session rather than the untouched rollout.
    `tail`, when set, reads only the rollout's last that many lines.
    """
    p = Path(path)
    if not p.exists():
        return []
    events: list[dict] = []
    try:
        rows = (d for d in transcripts._tail_lines(p, tail) if isinstance(d, dict)) if tail \
            else _records(p)
        for d in rows:
            t = d.get("type")
            ts = d.get("timestamp", "")
            if _before_clear(ts, since_ms):
                continue
            payload = d.get("payload") or {}

            if t == "event_msg":
                # The user's typed prompt is logged as a `user_message`
                # event with the text in `message` — or, on newer Codex, as
                # an `item_completed` event carrying a UserMessage item.
                # (role=user response_item turns mix in synthetic injections,
                # so they are never the source of a user row.)
                text = _event_user_text(payload)
                if text:
                    text = cap_text(text, MESSAGE_CHARS)
                    # A rollout speaks one of the two shapes, never both. If
                    # a future build emits both, the copies land back to back
                    # (only the ignored role=user record sits between them) —
                    # so drop a prompt that repeats the row just written.
                    prev = events[-1] if events else None
                    if not (prev and prev["kind"] == "user_text"
                            and prev["text"] == text):
                        events.append(transcripts.event(ts, "user_text", text, role="user"))

            elif t == "response_item":
                item_type = payload.get("type", "")
                if item_type in ("function_call", "custom_tool_call"):
                    # A custom tool call is a freeform one — newer Codex runs
                    # every shell command through `exec`, whose call body is
                    # a JS snippet in `input` rather than JSON `arguments`.
                    args = payload.get("arguments")
                    if item_type == "custom_tool_call":
                        args = payload.get("input")
                    events.append(transcripts.event(
                        ts, "tool_use", role="assistant", tool=payload.get("name", "function"),
                        extra={"arguments": cap_text(args, TOOL_ARG_CHARS)}))
                elif item_type in ("function_call_output", "custom_tool_call_output"):
                    events.append(transcripts.event(
                        ts, "tool_result",
                        cap_text(_tool_output_text(payload.get("output")), TOOL_RESULT_CHARS),
                        role="user"))
                elif item_type == "message":
                    content = payload.get("content")
                    if isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "output_text":
                                events.append(transcripts.event(
                                    ts, "assistant_text", cap_text(c.get("text"), MESSAGE_CHARS),
                                    role="assistant"))
    except Exception:
        pass
    return events[-limit:]


def live_timeline(w: Window, limit: int) -> dict:
    """A live Codex card's timeline: its `events` from the clear on (see
    mark_cleared), and the skills and memory its rollout touched."""
    if not w.transcript_path:
        return {"events": []}
    activity = extract_codex_session_activity(w.transcript_path)
    return {
        "events": codex_timeline(w.transcript_path, limit=limit, since_ms=cleared_at_ms(w.pid)),
        "skills_used": activity["skills_used"],
        "memory_ops": activity["memory_ops"],
    }


# ---------- live session discovery (running codex TUIs as dashboard cards) ----------

def _read_tail_events(path: Path, max_lines: int = 120) -> list[dict]:
    """Parse the last `max_lines` JSONL records of a rollout (newest last). A
    card reads it once a tick and hands it to the helpers below."""
    return transcripts._tail_lines(path, max_lines)


def _last_assistant_text(events: list[dict], since_ms: int = 0) -> str:
    """Most recent assistant output_text in a rollout's tail `events`, used as
    the card's current-task hint."""
    for d in reversed(events):
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


def _last_turn_error(events: list[dict], since_ms: int = 0) -> Optional[str]:
    """Human-readable error of the LATEST completed turn in a rollout's tail
    `events`, or None.

    Codex records each turn's outcome as an event_msg/task_complete; a failed
    turn carries `error.message`, usually a JSON blob whose text lives at
    error.message inside it (e.g. a 400 for an unsupported model). Only the most
    recent task_complete counts — a later successful turn clears the card. Repro:
    session 019f9feb, where every turn 400'd and the card showed nothing at all.
    """
    for d in reversed(events):
        if d.get("type") != "event_msg":
            continue
        payload = d.get("payload") or {}
        if payload.get("type") != "task_complete":
            continue
        if _before_clear(d.get("timestamp", ""), since_ms):
            return None
        err = payload.get("error")
        if not isinstance(err, dict):
            return None
        msg = str(err.get("message") or "").strip()
        if not msg:
            return None
        try:
            inner = json.loads(msg)
            msg = str(inner["error"]["message"]) or msg
        except Exception:
            pass
        return msg[:300]
    return None


def _infer_codex_status(events: list[dict], mtime: float) -> str:
    """busy | idle, inferred from the last substantive rollout event + mtime.

    Codex rollouts carry no explicit status field, so we look at the last
    meaningful event of the tail `events` (its last 60 records), skipping
    `token_count` telemetry noise:
      - a trailing tool call (a tool was issued, output pending) → busy
      - a rollout touched within the last few seconds → busy (actively writing)
      - otherwise → idle

    Newer Codex issues shell commands as `custom_tool_call`, so a session parked
    on a long `exec` reads as busy there too — not idle with a pending command.
    """
    if (time.time() - mtime) < _BUSY_MTIME_WINDOW:
        return "busy"
    last_kind = ""
    for d in events[-60:]:
        t = d.get("type", "")
        payload = d.get("payload") or {}
        if t == "event_msg" and payload.get("type") == "token_count":
            continue  # telemetry; not a real activity signal
        if t == "response_item":
            it = payload.get("type", "")
            if it in ("function_call", "function_call_output", "message",
                      "custom_tool_call", "custom_tool_call_output"):
                last_kind = it
        elif t == "event_msg":
            last_kind = "event_" + str(payload.get("role") or payload.get("type") or "")
    return "busy" if last_kind in ("function_call", "custom_tool_call") else "idle"


def _is_subagent_rollout(path: str) -> bool:
    """Whether this rollout belongs to a subagent thread rather than the user's.

    `spawn_agent` starts a child thread that writes its OWN rollout *from the
    same process*, so the parent's fd table holds both. Its session_meta names
    the parent (`parent_thread_id`) and records how it was started
    (`source: {"subagent": {"thread_spawn": …}}`); a top-level user thread has
    neither. Unreadable meta ⇒ False: a rollout we can't classify is treated as
    the user's, which at worst restores the old newest-fd behaviour.
    """
    meta = _parse_session_meta(Path(path))
    if not meta:
        return False
    if meta.get("parent_thread_id"):
        return True
    src = meta.get("source")
    return isinstance(src, dict) and "subagent" in src


def _newest_rollout_in_fd_dir(fd_dir: str, sessions_marker: str) -> Optional[str]:
    """The most recently written rollout JSONL among the fds open in `fd_dir`.

    A codex TUI that runs a turn to completion and then continues opens a *new*
    rollout while keeping the finished one's fd open — so a single process can
    hold several rollout fds at once. Returning whichever listdir yields first
    latches the card onto a stale, frozen transcript; pick the newest-by-mtime
    fd instead (the live rollout is the one still being written). mtime is read
    through the fd, which follows the symlink to the target file.

    Newest-by-mtime alone isn't enough, though: a `spawn_agent` subagent writes
    its own rollout from this same process, and while it runs *it* is the newest
    fd — so the card would swap to the subagent's freshly-opened (near-empty)
    thread mid-turn, blanking all of the user's history, then swap back when the
    subagent finished. So subagent rollouts only win when there is no user
    thread among the fds at all.
    """
    best: Optional[str] = None
    best_mtime = -1.0
    best_is_sub = True          # any user thread outranks any subagent thread
    try:
        names = os.listdir(fd_dir)
    except Exception:
        return None
    for n in names:
        fd_path = os.path.join(fd_dir, n)
        try:
            target = os.readlink(fd_path)
        except Exception:
            continue
        if "rollout-" in target and target.endswith(".jsonl") and sessions_marker in target:
            try:
                mtime = os.stat(fd_path).st_mtime
            except Exception:
                mtime = 0.0
            is_sub = _is_subagent_rollout(target)
            if (not is_sub, mtime) > (not best_is_sub, best_mtime):
                best, best_mtime, best_is_sub = target, mtime, is_sub
    return best


def _rollout_fd(pid: int) -> Optional[str]:
    """The codex rollout JSONL this pid is currently writing, or None.

    A running interactive codex session keeps its transcript fd open; the
    background `mcp-server`/`app-server` codex processes do not, so this check
    naturally selects only real user-facing sessions. When a process holds more
    than one rollout fd (a finished turn, its live continuation, a subagent's
    thread), the newest *user* thread wins — see _newest_rollout_in_fd_dir.
    """
    return _newest_rollout_in_fd_dir(f"/proc/{pid}/fd", str(CODEX_SESSIONS_DIR))


def _top_codex_ancestor(fd_pid: int, table: dict[int, Proc]) -> int:
    """Walk up from the fd-holding inner process to the launcher process.

    The launcher (e.g. `node … codex --yolo`) is the right pid to expose as the
    card: killing it tears down the whole session, and it shares the tty with
    the inner binary so tmux-backed controls still resolve.
    """
    tty = table[fd_pid].tty
    cur = fd_pid
    seen = {cur}
    while True:
        pp = table[cur].ppid
        info = table.get(pp)
        if not info or pp in seen:
            break
        if "codex" in info.args and info.tty == tty:
            cur = pp
            seen.add(cur)
        else:
            break
    return cur


# Codex subcommands that run headless/background, not an interactive TUI — these
# are spawned by editors or by Claude's codex MCP and shouldn't appear as cards.
_BG_SUBCOMMANDS = {"mcp-server", "app-server", "exec"}
# The options of `codex` itself that take a value, as `codex --help` lists them
# (codex-cli 0.160.0). The value has to be stepped over to reach the subcommand:
# the VS Code / Cursor extension runs `codex -c features.code_mode_host=true
# app-server`, whose `-c` value was taken for the subcommand — no background
# one, so the app server read as an interactive TUI. (`-i` takes one or more
# files; a second one is taken for the first word, and names no subcommand.)
_CODEX_VALUE_OPTS = frozenset({
    "-c", "--config", "--enable", "--disable", "--remote", "--remote-auth-token-env",
    "-i", "--image", "-m", "--model", "--local-provider", "-p", "--profile",
    "-s", "--sandbox", "-C", "--cd", "--add-dir", "-a", "--ask-for-approval",
})


def _is_interactive_codex(args: str | list[str]) -> bool:
    """True for an interactive Codex TUI process (`codex`, `codex --yolo`,
    `codex resume …`, `codex "a prompt"`); False for non-codex procs and
    background subcommands. `args` is a `ps` args string or the real argv."""
    cli = _cli_words(args, "codex", _CODEX_VALUE_OPTS)
    return cli is not None and cli[0] not in _BG_SUBCOMMANDS


def list_codex_windows() -> list[Window]:
    """Discover running interactive Codex sessions as Window objects.

    Detection is process-first (grouped by controlling tty) rather than purely
    fd-based: a freshly launched `codex` doesn't open its rollout transcript
    until the first turn, so a card must appear from the live process alone and
    get its session_id/transcript filled in once the rollout exists.

    Linux-only (reads /proc); returns [] on any platform without it.
    """
    return [w for w, _ in _discover()]


def _discover() -> list[tuple[Window, list[dict]]]:
    """list_codex_windows, each window with the tail of its rollout ([] before
    it has one) — read once here for the status, and handed on so
    codex_window_dicts reads the card's details off the same tail."""
    if not Path("/proc").is_dir():
        return []
    table = proc_table()

    # Group interactive codex processes (launcher + inner binary) by their tty;
    # one foreground tty == one session.
    by_tty: dict[str, list[int]] = {}
    for pid, info in table.items():
        tty = _ps_tty(info.tty)
        if not tty:
            continue
        # On the real argv (see _proc_argv), read only for a codex: ps joins it
        # with spaces, which splits a `-c` value that has one and makes a
        # prompt that begins "exec …" read as the subcommand.
        if (_exe_index(info.args.split(), "codex") < 0
                or not _is_interactive_codex(_proc_argv(pid, info.args))):
            continue
        by_tty.setdefault(tty, []).append(pid)

    windows: list[tuple[Window, list[dict]]] = []
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

        cwd = next(filter(None, map(_proc_cwd, (anchor, card_pid, *pids))), "")
        meta = (_parse_session_meta(Path(rollout)) or {}) if rollout else {}
        cwd = cwd or meta.get("cwd", "") or ""
        # The machine-local filter (CLAUDE_FLEET_CWD_INCLUDE/EXCLUDE), as every
        # other card has it — on both cwds a Codex card has: the one it shows,
        # and the one its rollout records, whose timeline the card serves. They
        # need not agree (`codex resume --all` picks up a session started in any
        # dir), and the archive judges that rollout on the cwd it records.
        if not _cwd_visible(cwd) or (rollout and not transcript_visible(rollout)):
            continue

        # Anchor the card's ordering to the immutable process start time, NOT the
        # rollout meta timestamp. The two clocks disagree (e.g. `codex resume`
        # reuses an old rollout whose meta predates the process), and the rollout
        # branch is only taken while a pid is observed holding the transcript fd —
        # so a session that flips between the rollout and the just-launched
        # fallback would see started_at jump by hours, reshuffling its card mid-
        # turn. proc start is constant for the life of card_pid (which is itself
        # the stable card :key), so the card stays put busy↔idle. Frontend sorts
        # cards by started_at and never displays it, so this is ordering-only.
        started_at = _proc_start_ms(card_pid)

        events: list[dict] = []
        if rollout:
            rp = Path(rollout)
            try:
                mtime = rp.stat().st_mtime
            except Exception:
                mtime = None
            events = _read_tail_events(rp)
            status = _infer_codex_status(events, mtime) if mtime else "idle"
            updated_at = int(mtime * 1000) if mtime else started_at
            session_id = meta.get("id", rp.stem)
            version = str(meta.get("cli_version", ""))
            transcript = str(rp)
        else:
            # Just launched: no rollout yet. Show the card anyway, keyed by pid.
            status = "idle"
            updated_at = started_at
            session_id = f"codex-{card_pid}"
            version = ""
            transcript = None

        windows.append((Window(
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
            # The launcher shares the tty its group was found on (see
            # _top_codex_ancestor).
            tty=tty,
            transcript_path=transcript,
            alive=True,
            hidden=False,
            platform="codex",
        ), events))

    windows.sort(key=lambda we: (-we[0].updated_at, we[0].pid))
    return windows


def codex_window_dicts() -> list[dict]:
    """Live codex windows as fully-enriched dashboard dicts (skills/memory/
    triage/current_task), ready to merge into the snapshot alongside Claude
    windows. Shell-process counts are filled in by the caller (platform-agnostic).
    """
    out: list[dict] = []
    for w, events in _discover():
        d = w.to_dict()
        tp = Path(w.transcript_path) if w.transcript_path else None
        since = cleared_at_ms(w.pid)
        activity = extract_codex_session_activity(tp) if tp else {}
        current_task = _last_assistant_text(events, since)
        d.update({
            "permission_msg": None,
            "permission_ts": None,
            "first_input": (_extract_first_user_input(tp, since) if tp else "")[:100],
            "current_task": current_task or None,
            "last_error": _last_turn_error(events, since),
            **patrol.classify_idle(w.status, d.get("idle_seconds", 0), current_task),
            "skills_used": activity.get("skills_used", []),
            "memory_ops": activity.get("memory_ops", []),
            "background_tasks": [],
            "queued": [],
            # What the last turn ran on, per the rollout. The card prefers the
            # pane's status line (app._local_snapshot), which also knows about a
            # switch no turn has run on yet; this is the readout without a pane.
            "model": activity.get("model", ""),
            "effort": activity.get("effort", ""),
            "model_label": " ".join(x for x in (activity.get("model", ""), activity.get("effort", "")) if x),
            "model_source": "transcript" if activity.get("model") else "",
        })
        out.append(d)
    return out

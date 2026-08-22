"""Parse ~/.claude/projects/{slug}/{sessionId}.jsonl transcripts."""
from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .textcap import (
    GOAL_CHARS,
    LOOP_CHARS,
    META_CHARS,
    MESSAGE_CHARS,
    TOOL_ARG_CHARS,
    TOOL_RESULT_CHARS,
    cap_text,
    edit_diff,
)

_CMD_NAME_RE = re.compile(r"<command-name>\s*(.*?)\s*</command-name>", re.DOTALL)
_CMD_ARGS_RE = re.compile(r"<command-args>\s*(.*?)\s*</command-args>", re.DOTALL)
# Claude appends this to a recap for the first while, as a TUI affordance. The
# board has no /config, so on a card it's just a line of noise repeated on every
# recap.
_RECAP_HINT_RE = re.compile(r"\s*\(\s*disable recaps in /config\s*\)\s*$")

# Envelopes the CLI writes into a `user` turn that nobody typed, and that carry
# no `isMeta` — so the text is the only marker there is. `<task-notification>` is
# a background task reporting back; `<local-command-stdout>` is a slash command's
# own output echoed into the turn.
_INJECTED_ENVELOPES = ("<task-notification>", "<local-command-stdout>")
_TN_STATUS_RE = re.compile(r"<status>\s*(.*?)\s*</status>", re.DOTALL)
_TN_SUMMARY_RE = re.compile(r"<summary>\s*(.*?)\s*</summary>", re.DOTALL)


def is_injected_user_row(d: dict) -> bool:
    """True when a `user` row is the harness talking, not a person typing.

    Three row-level markers, because the CLI added them at different times and a
    fleet runs several versions at once:

      - `isMeta` — a skill's SKILL.md body, a local-command caveat.
      - `promptSource: "system"` — the harness speaking in the user's turn.
      - `origin.kind == "task-notification"` — a background task reporting back.

    A prompt typed on an older CLI carries none of the three, so the test has to
    stay negative: unmarked means typed. `is_injected_text` catches the rows
    that arrive with no marker at all.
    """
    if d.get("isMeta") or d.get("promptSource") == "system":
        return True
    origin = d.get("origin")
    return isinstance(origin, dict) and origin.get("kind") == "task-notification"


def is_injected_text(text: str) -> bool:
    """True when a user row opens with an envelope no person types."""
    return text.lstrip().startswith(_INJECTED_ENVELOPES)


def _clean_task_notification(text: str) -> str:
    """A background-task notification as the one line of it worth reading.

    The harness wraps five XML fields, and the two that say anything — status and
    summary — are the last two: the task id, the tool-use id and the output path
    ahead of them are addressed to the model. Injected rows are capped short, so
    left whole the row spends its whole budget on those ids and cuts off right
    before the summary.
    """
    if not is_injected_text(text) or "<task-notification>" not in text:
        return text
    status = _TN_STATUS_RE.search(text)
    summary = _TN_SUMMARY_RE.search(text)
    if not status and not summary:
        return text
    head = f"后台任务 {status.group(1)}" if status else "后台任务"
    return f"{head}：{summary.group(1)}" if summary else head


def _clean_command_text(text: str) -> str:
    """Render a slash-command envelope as a bare command label.

    Claude logs `/clear` as `<command-name>/clear</command-name>...`. The board
    shows the raw text, so strip the envelope down to `clear` (no leading slash),
    appending any command args. Non-command text is returned unchanged.
    """
    m = _CMD_NAME_RE.search(text)
    if not m:
        return text
    name = m.group(1).lstrip("/")
    args_m = _CMD_ARGS_RE.search(text)
    args = args_m.group(1).strip() if args_m else ""
    return f"{name} {args}".strip()


def clean_user_text(text: str) -> str:
    """A user row's envelope reduced to what it actually says."""
    return _clean_task_notification(_clean_command_text(text))


@dataclass
class TurnEvent:
    ts: str
    kind: str            # user_text | assistant_text | tool_use | tool_result | system
    text: str            # ≤ 4 KB excerpt
    tool: Optional[str]  # name of tool when kind == tool_use
    role: str            # user | assistant | system
    extra: dict          # small structured payload (e.g. tool input keys)


def _iter_lines(path: Path) -> Iterable[dict]:
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue
    except FileNotFoundError:
        return


def _tail_lines(path: Path, n: int) -> list[dict]:
    buf: deque[dict] = deque(maxlen=n)
    for d in _iter_lines(path):
        buf.append(d)
    return list(buf)


def _flatten_assistant(msg: dict) -> list[TurnEvent]:
    out: list[TurnEvent] = []
    content = msg.get("content") or []
    ts = msg.get("timestamp") or ""
    if isinstance(content, str):
        out.append(TurnEvent(ts, "assistant_text", cap_text(content, MESSAGE_CHARS), None, "assistant", {}))
        return out
    if not isinstance(content, list):
        return out
    for c in content:
        ct = c.get("type")
        if ct == "text":
            out.append(TurnEvent(ts, "assistant_text", cap_text(c.get("text"), MESSAGE_CHARS), None, "assistant", {}))
        elif ct == "tool_use":
            inp = c.get("input") or {}
            tool_name = c.get("name", "")
            file_path = str(inp.get("file_path", ""))

            if tool_name == "Skill":
                skill_name = inp.get("skill", "")
                out.append(TurnEvent(
                    ts, "skill_invoke", "", skill_name, "assistant",
                    {"args": cap_text(inp.get("args"), TOOL_ARG_CHARS)},
                ))
            elif tool_name in ("Read", "Write", "Edit") and "/memory/" in file_path:
                mem_name = file_path.rsplit("/", 1)[-1].replace(".md", "")
                kind = "memory_write" if tool_name in ("Write", "Edit") else "memory_read"
                out.append(TurnEvent(
                    ts, kind, "", mem_name, "assistant",
                    {"operation": tool_name.lower(), "path": file_path},
                ))
            elif tool_name == "Edit" and isinstance(inp, dict):
                # The generic branch below caps each arg on its own, and an
                # Edit's two strings start out identical — all 200 chars go to
                # the shared prefix and the row shows everything except the
                # edit. Send the diff of the two instead of the two.
                extra = {
                    "file_path": file_path,
                    "diff": edit_diff(inp.get("old_string"), inp.get("new_string")),
                }
                if inp.get("replace_all"):
                    extra["replace_all"] = True
                out.append(TurnEvent(ts, "tool_use", "", tool_name, "assistant", extra))
            elif tool_name == "AskUserQuestion":
                # `questions` is a list, so the generic branch below would collapse
                # it to `<list>` and drop every option. Expand it into a structured
                # payload (+ readable text fallback) so the transcript shows the
                # full question and each option's label/description.
                q_payload: list[dict] = []
                text_lines: list[str] = []
                raw_qs = inp.get("questions") if isinstance(inp, dict) else None
                for q in (raw_qs or []):
                    if not isinstance(q, dict):
                        continue
                    opts = []
                    for o in (q.get("options") or []):
                        if isinstance(o, dict):
                            opts.append({
                                "label": str(o.get("label", ""))[:200],
                                "desc": str(o.get("description", ""))[:400],
                            })
                    qtext = cap_text(str(q.get("question", "")), MESSAGE_CHARS)
                    q_payload.append({
                        "q": qtext,
                        "header": str(q.get("header", ""))[:60],
                        "multi": bool(q.get("multiSelect", False)),
                        "options": opts,
                    })
                    text_lines.append(qtext)
                    for o in opts:
                        text_lines.append(
                            f"  • {o['label']}" + (f" — {o['desc']}" if o["desc"] else "")
                        )
                out.append(TurnEvent(
                    ts, "ask_question", cap_text("\n".join(text_lines), MESSAGE_CHARS), tool_name,
                    "assistant", {"questions": q_payload},
                ))
            else:
                preview: dict = {}
                for k, v in (inp.items() if isinstance(inp, dict) else []):
                    if isinstance(v, str):
                        preview[k] = cap_text(v, TOOL_ARG_CHARS)
                    elif isinstance(v, (int, float, bool)) or v is None:
                        preview[k] = v
                    else:
                        preview[k] = f"<{type(v).__name__}>"
                    if len(preview) >= 6:
                        break
                out.append(TurnEvent(ts, "tool_use", "", tool_name, "assistant", preview))
        elif ct == "thinking":
            # Skip thinking — too noisy for dashboard.
            continue
    return out


def _flatten_user(msg: dict, is_meta: bool = False) -> list[TurnEvent]:
    out: list[TurnEvent] = []
    content = msg.get("content") or []
    ts = msg.get("timestamp") or ""

    def user_row(raw: str) -> TurnEvent:
        # A prompt someone typed is the point of the timeline, so it gets the
        # generous cap. Anything the harness wrote in the user's turn — a skill
        # body, a command caveat, a background task reporting back — is worth a
        # trace, not a wall, and is flagged so the board can tell the two apart.
        injected = is_meta or is_injected_text(raw)
        return TurnEvent(
            ts, "user_text",
            cap_text(clean_user_text(raw), META_CHARS if injected else MESSAGE_CHARS),
            None, "user", {"meta": True} if injected else {},
        )

    if isinstance(content, str):
        out.append(user_row(content))
        return out
    if not isinstance(content, list):
        return out
    for c in content:
        ct = c.get("type")
        if ct == "text":
            out.append(user_row(c.get("text") or ""))
        elif ct == "tool_result":
            content_val = c.get("content")
            if isinstance(content_val, list):
                text_parts = [x.get("text", "") for x in content_val if isinstance(x, dict)]
                full = " ".join(text_parts)
            else:
                full = str(content_val)
            # Sensitive: don't dump full stdout — keep tool output at the short
            # TOOL_RESULT_CHARS cap. The exception is the AskUserQuestion answer
            # echo, which is just the user's own selection (question + chosen
            # option), not tool output, and is worth seeing whole.
            limit = (MESSAGE_CHARS if full.startswith("Your questions have been answered")
                     else TOOL_RESULT_CHARS)
            out.append(TurnEvent(ts, "tool_result", cap_text(full, limit), None, "user", {}))
    return out


def _flatten_queued_prompt(d: dict) -> list[TurnEvent]:
    """A prompt typed while the session was busy, as the user row it never got.

    A queued prompt reaches Claude one of two ways. If the queue drains at the
    end of a turn, Claude logs an ordinary `user` row and the timeline already
    has it. If Claude pulls it off *mid*-turn instead, the only trace is this
    `queued_command` attachment (plus `queue-operation` bookkeeping): no user row
    is ever written, so the prompt was missing from the board while the answer to
    it stayed — which reads as "I said something and the timeline lost it".

    The row lands where Claude read the prompt, but `ts` is when it was typed, so
    a prompt that waited out a long tool call shows a time earlier than the row
    above it. `extra.queued` lets the client say why instead of looking like a
    clock that ran backwards.

    Claude queues its own background-task notifications the same way, and they
    outnumber typed prompts here about four to one. Nothing on the attachment
    says which is which, so the envelope in the text is what marks them.
    """
    a = d.get("attachment") or {}
    if a.get("type") != "queued_command":
        return []
    text = a.get("prompt") or ""
    if not text.strip():
        return []
    extra = {"queued": True}
    if is_injected_text(text):
        extra["meta"] = True
    return [TurnEvent(
        d.get("timestamp") or a.get("timestamp") or "", "user_text",
        cap_text(clean_user_text(text), META_CHARS if extra.get("meta") else MESSAGE_CHARS),
        None, "user", extra,
    )]


def _flatten_recap(d: dict) -> list[TurnEvent]:
    """The recap Claude writes when you've been away, as a timeline row.

    Claude logs it as `system`/`away_summary`: two or three sentences of where
    the session got to and what it needs next. The board is read by someone who
    was away by definition, and that summary is the single most useful row in the
    transcript for them — but every `system` row currently flattens to the same
    placeholder the client hides, so it was the one thing the timeline dropped.

    Its own `kind`, not `assistant_text`: nobody asked for it, it isn't part of
    the turn it sits in, and the client styles it as the standing "where we are"
    rather than as another reply.
    """
    text = _RECAP_HINT_RE.sub("", d.get("content") or "").strip()
    if not text:
        return []
    return [TurnEvent(
        d.get("timestamp", ""), "recap", cap_text(text, MESSAGE_CHARS),
        None, "system", {},
    )]


# "Goal: ship X", "Goal is shipping X", "目标：交付 X". Claude labels the goal
# this way in about half the recaps it writes; the other half state the same
# thing without the label, and guessing which opening sentence counts as a goal
# would put a wrong one on permanent display. Only the labelled ones are taken.
_GOAL_RE = re.compile(r"^\s*(?:goal|目标|本次目标)\s*(?:is\s+|[:：是]\s*)(.+)$",
                      re.IGNORECASE | re.DOTALL)
# End of the first sentence. A semicolon always ends it — Claude uses one to
# hang the current status off the goal ("…on G1 data; the plan is written"), and
# nothing else puts one mid-sentence. A period only ends it when a new sentence
# follows (whitespace, then a capital) or the text does: that lookahead is what
# keeps "pi0.5" and "53.8%" from cutting a goal off one word in.
_SENT_END_RE = re.compile(r"[。！？；;]|[.!?](?=\s+[A-Z(\[\"'])|[.!?]\s*$")


def _first_sentence(text: str) -> str:
    m = _SENT_END_RE.search(text)
    return (text[:m.start()] if m else text).strip()


# A prompt opened with "goal …" — the way this fleet actually sets one, and the
# only goal a session running CLI ≥ 2.1.221 has, since that build stopped writing
# recaps. `_clean_command_text` has already unwrapped a real `/goal` by the time
# this sees it, so both spellings land here.
_PROMPT_GOAL_RE = re.compile(r"^\s*goal\b[:：]?\s+(.+)$", re.IGNORECASE | re.DOTALL)


def session_goal(path: str | Path) -> Optional[dict]:
    """What this session is for, as {text, ts, source} — None if it never said.

    Two sources, newest wins, because either can be the only one present:

      - `recap`: Claude opens a recap with "Goal: …" when the session has a
        standing objective, and repeats it as the work moves. Taken from the
        newest *labelled* recap rather than simply the newest one, since a later
        recap can drop the label while the goal still stands.
      - `prompt`: a prompt the user opened with "goal …". Recaps are the better
        source when they exist — Claude keeps them current — but they stopped
        being written after CLI 2.1.220, so on a session running today this is
        all there is.

    Either way the point is to outlive the timeline. A goal is stated once and
    then buried under a few hundred tool rows, which is exactly when someone
    opens the board to ask what a session is even doing.
    """
    p = Path(path)
    if not p.exists():
        return None
    goal: Optional[dict] = None

    def offer(text: str, ts: str, source: str) -> None:
        nonlocal goal
        text = _first_sentence(text)
        if not text:
            return
        # Ties go to whatever came later in the file: a recap is written after
        # the turn it summarizes, so on an equal stamp it is the fresher word.
        if goal is None or _parse_ts(ts) >= _parse_ts(goal["ts"]):
            goal = {"text": cap_text(text, GOAL_CHARS), "ts": ts, "source": source}

    for d in _iter_lines(p):
        if d.get("type") == "system" and d.get("subtype") == "away_summary":
            content = _RECAP_HINT_RE.sub("", d.get("content") or "").strip()
            m = _GOAL_RE.match(content)
            if m:
                offer(m.group(1), d.get("timestamp", ""), "recap")
            continue
        # Anything the user typed, including a prompt Claude took off its queue.
        for ev in _normalize(d):
            if ev.kind != "user_text" or ev.extra.get("meta"):
                continue
            m = _PROMPT_GOAL_RE.match(ev.text)
            if m:
                offer(m.group(1), ev.ts, "prompt")
    return goal


_CRON_STEP_RE = re.compile(r"^\*/(\d+)$")
_CRON_LIST_RE = re.compile(r"^\d+(?:,\d+)*$")


def _cron_every(field: str, period: int) -> Optional[int]:
    """The gap between one cron field's firings, or None if they aren't even.

    Two spellings mean the same cadence. `*/30` is the obvious one; `7,37` is
    what a model writes to spread a fleet's loops across the hour instead of
    firing them all on the same minute, and it is still every 30 minutes. A list
    only counts when it wraps evenly too: `0,10` fires twice an hour, fifty
    minutes apart, and has no single cadence worth printing.
    """
    m = _CRON_STEP_RE.match(field)
    if m:
        step = int(m.group(1))
        return step if 0 < step < period else None
    if not _CRON_LIST_RE.match(field):
        return None
    vals = sorted({int(v) for v in field.split(",")})
    if len(vals) < 2 or vals[-1] >= period:
        return None
    gaps = {b - a for a, b in zip(vals, vals[1:])}
    gaps.add(period - vals[-1] + vals[0])
    return gaps.pop() if len(gaps) == 1 else None


def _cron_cadence(expr: str) -> str:
    """A cron expression as the cadence it was set for, e.g. "每 30 分钟".

    Only the shapes `/loop` emits are read. Anything else is handed back as the
    expression itself: an unparsed cadence reads as unparsed, while a guessed one
    reads as fact and would sit pinned above the timeline being wrong.
    """
    parts = expr.split()
    if len(parts) != 5:
        return expr
    minute, hour, dom, mon, dow = parts
    if (mon, dow) != ("*", "*"):
        return expr
    if dom == "*" and hour == "*":
        n = _cron_every(minute, 60)
        return f"每 {n} 分钟" if n else expr
    if dom == "*" and minute.isdigit():
        n = _cron_every(hour, 24)
        if n:
            return f"每 {n} 小时"
        if hour.isdigit():
            return f"每天 {int(hour):02d}:{int(minute):02d}"
        return expr
    m = _CRON_STEP_RE.match(dom)
    if m and minute.isdigit() and hour.isdigit():
        return f"每 {m.group(1)} 天"
    return expr


def _delay_cadence(seconds) -> Optional[str]:
    """A ScheduleWakeup delay as a cadence. The tool clamps to one hour, so
    minutes is the only unit this ever has to reach for."""
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return None
    return f"自定节奏 · 约 {max(1, round(s / 60))} 分钟" if s > 0 else None


# `/loop` hands the next firing its own invocation back, so a self-paced loop's
# stored prompt still carries the slash command it re-enters through.
_LOOP_SELF_PREFIX_RE = re.compile(r"^\s*/loop\s+", re.IGNORECASE)
# What `/loop` stores when nobody typed a task: the runtime swaps the real
# instructions in at fire time, so the sentinel itself says nothing to a reader.
_LOOP_SENTINELS = ("<<autonomous-loop-dynamic>>", "<<autonomous-loop>>")
# A `/loop` invocation, matched on the envelope rather than on the unwrapped
# text. `_clean_command_text` renders it as "loop 30 mins, …", which is
# indistinguishable from prose opening the same way — and "loop through the
# files and fix each" is a sentence, not a schedule. Only the slash command (or
# the Skill call a model makes instead) starts a loop, so only those count.
_LOOP_CMD_RE = re.compile(r"<command-name>\s*/?loop\s*</command-name>", re.IGNORECASE)


def _loop_task_text(prompt: str) -> str:
    """A stored wakeup prompt as the task, minus the `/loop` wrapper."""
    text = _LOOP_SELF_PREFIX_RE.sub("", prompt).strip()
    return "自主循环" if text in _LOOP_SENTINELS else text


def session_loop(path: str | Path) -> Optional[dict]:
    """The prompt this session keeps re-running, as {text, ts, source, cadence}.

    Like a goal, a loop is set once and then buried — except it goes on acting on
    the session long after the row that started it scrolled away, which is what
    makes an unpinned one confusing: turns keep arriving that nobody just asked
    for. Three sources, last one in the file wins, since the file is in the order
    the loop was actually set up:

      - `cron`: a recurring `CronCreate`, the fixed-interval mode. The best
        source there is — the schedule and the prompt are the ones registered,
        not the ones asked for, and those differ (`/loop 30 mins …` registers
        `7,37 * * * *` and drops the interval from the prompt).
      - `wakeup`: a `ScheduleWakeup`, the self-paced mode. Re-armed every tick,
        so the stamp tracks the most recent arming rather than the first.
      - `prompt`: a `/loop` invocation, typed or made through the Skill tool.
        Covers the gap between asking for a loop and the model scheduling one —
        and the case where it never did, which is why this source carries no
        cadence to claim.

    A stopped loop returns None rather than a stale banner: `CronDelete` ends a
    cron, `ScheduleWakeup(stop=True)` ends a self-paced one, and either way
    there is nothing left to pin.
    """
    p = Path(path)
    if not p.exists():
        return None
    loop: Optional[dict] = None

    def offer(text: str, ts: str, source: str, cadence: Optional[str]) -> None:
        nonlocal loop
        text = text.strip()
        if not text:
            return
        loop = {"text": cap_text(text, LOOP_CHARS), "ts": ts,
                "source": source, "cadence": cadence}

    for d in _iter_lines(p):
        if d.get("type") == "assistant":
            ts = d.get("timestamp", "")
            for c in ((d.get("message") or {}).get("content") or []):
                if not isinstance(c, dict) or c.get("type") != "tool_use":
                    continue
                name, inp = c.get("name", ""), (c.get("input") or {})
                if name == "Skill" and str(inp.get("skill", "")).lstrip("/") == "loop":
                    offer(str(inp.get("args") or ""), ts, "prompt", None)
                elif name == "CronDelete":
                    loop = None
                elif name == "CronCreate" and inp.get("recurring"):
                    offer(str(inp.get("prompt") or ""), ts, "cron",
                          _cron_cadence(str(inp.get("cron") or "")))
                elif name == "ScheduleWakeup":
                    if inp.get("stop"):
                        loop = None
                    else:
                        offer(_loop_task_text(str(inp.get("prompt") or "")), ts,
                              "wakeup", _delay_cadence(inp.get("delaySeconds")))
            continue
        # A `/loop` someone typed. Read off the raw row: the envelope is the
        # only thing that distinguishes the command from a sentence about loops.
        if d.get("type") != "user" or is_injected_user_row(d):
            continue
        raw = (d.get("message") or {}).get("content")
        if isinstance(raw, list):
            raw = "".join(b.get("text", "") for b in raw
                          if isinstance(b, dict) and b.get("type") == "text")
        if isinstance(raw, str) and _LOOP_CMD_RE.search(raw):
            args = _CMD_ARGS_RE.search(raw)
            offer(args.group(1) if args else "", d.get("timestamp", ""), "prompt", None)
    return loop


def _normalize(d: dict) -> list[TurnEvent]:
    t = d.get("type")
    msg = d.get("message") or {}
    # `timestamp` lives on the outer envelope, not inside `message`.
    if msg and "timestamp" not in msg and d.get("timestamp"):
        msg["timestamp"] = d.get("timestamp")
    if t == "assistant":
        return _flatten_assistant(msg)
    if t == "user":
        # The markers sit on the envelope, not on `message`.
        return _flatten_user(msg, is_injected_user_row(d))
    if t == "attachment":
        return _flatten_queued_prompt(d)
    if t == "system" and d.get("subtype") == "away_summary":
        return _flatten_recap(d)
    if t in {"system", "permission-mode"}:
        return [TurnEvent(
            d.get("timestamp", ""), "system",
            t + (": " + str(d.get("permissionMode", "")) if d.get("permissionMode") else ""),
            None, "system", {}
        )]
    return []


def _row_model(d: dict) -> str:
    """The model that ran an assistant row ("" if none did).

    Claude stamps model "<synthetic>" on rows it writes on the model's behalf —
    "No response requested.", API-error placeholders. Counting those would park a
    card on "<synthetic>" and fake a switch away and back around every one.
    """
    if d.get("type") != "assistant":
        return ""
    model = (d.get("message") or {}).get("model") or ""
    return "" if model.startswith("<") else model


def current_model(path: str | Path) -> str:
    """Model id of the session's most recent assistant turn ("" if none yet).

    The last row, not the first (history._extract_model): a session that switched
    model mid-run is *on* the new one, and that's what the board's card claims.
    """
    p = Path(path)
    model = ""
    for d in _iter_lines(p):
        model = _row_model(d) or model
    return model


def pretty_model(raw: str) -> str:
    """Model id -> the label Claude's own /model dialog uses: claude-opus-4-8 ->
    "Opus 4.8". Anything that doesn't parse is passed through unchanged."""
    if not (raw or "").startswith("claude-"):
        return raw
    parts = [t for t in raw.split("-") if t and t != "claude"]
    # Drop the release date suffix (claude-haiku-4-5-20251001).
    parts = [t for t in parts if not (t.isdigit() and len(t) == 8)]
    family = next((t for t in parts if not t.isdigit()), "")
    version = ".".join(t for t in parts if t.isdigit())
    if not family or not version:
        return raw
    return f"{family.capitalize()} {version}"


def _model_change_events(raw: list[dict]) -> dict[int, TurnEvent]:
    """Model switches within `raw`, as {index of the row that first ran on the new
    model: event}.

    Read from the assistant rows because that's the model that actually answered:
    a switch that failed to land leaves no trace here (correctly), and one made by
    typing /model in the TUI shows up all the same. The first model seen isn't a
    change — `raw` is only a tail, so its earliest row has no predecessor.
    """
    out: dict[int, TurnEvent] = {}
    prev = ""
    for i, d in enumerate(raw):
        model = _row_model(d)
        if not model:
            continue
        if prev and model != prev:
            out[i] = TurnEvent(
                d.get("timestamp", ""), "model", f"Model → {pretty_model(model)}",
                None, "system", {"model": model},
            )
        prev = model
    return out


def timeline(path: str | Path, limit: int = 50) -> list[dict]:
    """Return ≤ limit most recent flattened turn events for a transcript."""
    p = Path(path)
    if not p.exists():
        return []
    # Read more lines than needed because one jsonl row can expand into several events.
    raw = _tail_lines(p, max(limit * 2, 100))
    switches = _model_change_events(raw)
    events: list[TurnEvent] = []
    for i, d in enumerate(raw):
        if i in switches:
            events.append(switches[i])
        events.extend(_normalize(d))
    return [e.__dict__ for e in events[-limit:]]


def _parse_ts(ts: str) -> float:
    """ISO-8601 timestamp string -> epoch seconds, or 0.0 if unparseable."""
    if not ts:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def consumed_prompt_texts(path: str | Path, since: float) -> list[tuple[float, str]]:
    """Prompts Claude has demonstrably taken, as (epoch_ts, text) at/after `since`.

    Used to reconcile the dashboard's sent-prompt queue. Two independent signals
    count as "Claude took it", and both are needed:

      - a genuine user_text row — the session was idle, so Claude read the prompt
        straight away and logged it as a normal user turn.
      - a `queue-operation` row with operation `remove` — the session was BUSY, so
        the prompt went into Claude's own queue (`enqueue`) and was later pulled
        off it (`remove`). These prompts never get a user row at all, so a
        user-row-only reconcile leaves every busy-time send stuck on the card
        forever. An `enqueue` with no `remove` is still waiting: not consumed.

    Scans the whole transcript rather than a fixed tail: a busy session buries
    the row under hundreds of tool rows, and anything queued has to stay
    matchable until Claude gets to it. `since` (the oldest pending send) bounds
    the result, so an old identical prompt can't clear a freshly queued one.
    """
    p = Path(path)
    if not p.exists():
        return []
    out: list[tuple[float, str]] = []
    for d in _iter_lines(p):
        if d.get("type") == "queue-operation":
            if d.get("operation") != "remove":
                continue
            ts, text = _parse_ts(d.get("timestamp", "")), d.get("content") or ""
            if ts >= since and text.strip():
                out.append((ts, text))
            continue
        for ev in _normalize(d):
            # A queued prompt's `queued_command` attachment is the same delivery
            # the `remove` row above already counted. Counting it twice would let
            # one prompt clear two identical copies off the card, one of which is
            # still waiting in Claude's queue.
            if ev.kind == "user_text" and ev.text.strip() and not ev.extra.get("queued"):
                ts = _parse_ts(ev.ts)
                if ts >= since:
                    out.append((ts, ev.text))
    return out


def current_task_hint(path: str | Path) -> Optional[str]:
    """Best-effort one-liner of what this session is currently doing."""
    p = Path(path)
    if not p.exists():
        return None
    raw = _tail_lines(p, 30)
    # Walk back to the most informative event.
    for d in reversed(raw):
        for ev in reversed(_normalize(d)):
            if ev.kind == "tool_use" and ev.tool:
                key_args = ", ".join(f"{k}={v!r}" for k, v in list(ev.extra.items())[:2])
                return f"{ev.tool}({key_args})" if key_args else ev.tool
            if ev.kind == "assistant_text" and ev.text.strip():
                first = ev.text.strip().splitlines()[0]
                return first[:160]
            # Only what a person typed: a task notification or a skill body is
            # the harness talking, and reads as a session doing nothing at all.
            if ev.kind == "user_text" and ev.text.strip() and not ev.extra.get("meta"):
                first = ev.text.strip().splitlines()[0]
                return f"↳ {first[:160]}"
    return None


def extract_skills_used(path: str | Path) -> list[str]:
    """Extract unique skill names invoked via the Skill tool."""
    counts = count_skill_invocations(path)
    return list(counts.keys())


def count_skill_invocations(path: str | Path) -> dict[str, int]:
    """Count total invocations per skill (not deduplicated)."""
    activity = count_skill_activity(path)
    return activity.get("per_skill_invokes", {})


def count_skill_activity(path: str | Path) -> dict:
    """Count all skill-related activity: invocations + file ops + bash refs.

    Returns {
        per_skill_invokes: {name: count},
        per_skill_file_ops: {name: count},
        per_skill_bash_refs: {name: count},
        totals: {invoke, file_ops, bash_refs, total},
    }
    """
    import re
    p = Path(path)
    if not p.exists():
        return {"per_skill_invokes": {}, "per_skill_file_ops": {},
                "per_skill_reads": {}, "per_skill_writes": {},
                "per_skill_bash_refs": {}, "totals": {"invoke": 0, "file_ops": 0, "reads": 0, "writes": 0, "bash_refs": 0, "total": 0}}

    invokes: dict[str, int] = {}
    file_ops: dict[str, int] = {}
    skill_reads: dict[str, int] = {}
    skill_writes: dict[str, int] = {}
    bash_refs: dict[str, int] = {}
    skill_path_re = re.compile(r'/\.claude/skills/([^/]+)/')

    for d in _iter_lines(p):
        if d.get("type") != "assistant":
            continue
        content = (d.get("message") or {}).get("content", [])
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict) or c.get("type") != "tool_use":
                continue
            name = c.get("name", "")
            inp = c.get("input") or {}

            if name == "Skill":
                sk = inp.get("skill", "")
                if sk:
                    invokes[sk] = invokes.get(sk, 0) + 1

            elif name in ("Read", "Write", "Edit"):
                fp = str(inp.get("file_path", ""))
                m = skill_path_re.search(fp)
                if m:
                    sk = m.group(1)
                    file_ops[sk] = file_ops.get(sk, 0) + 1
                    if name == "Read":
                        skill_reads[sk] = skill_reads.get(sk, 0) + 1
                    else:
                        skill_writes[sk] = skill_writes.get(sk, 0) + 1

            elif name == "Bash":
                cmd = str(inp.get("command", ""))
                if "skills/" in cmd or "SKILL.md" in cmd:
                    matches = skill_path_re.findall(cmd)
                    if matches:
                        for sk in set(matches):
                            bash_refs[sk] = bash_refs.get(sk, 0) + 1
                    else:
                        bash_refs["_general"] = bash_refs.get("_general", 0) + 1

    ti = sum(invokes.values())
    tf = sum(file_ops.values())
    tr = sum(skill_reads.values())
    tw = sum(skill_writes.values())
    tb = sum(bash_refs.values())
    return {
        "per_skill_invokes": invokes,
        "per_skill_file_ops": file_ops,
        "per_skill_reads": skill_reads,
        "per_skill_writes": skill_writes,
        "per_skill_bash_refs": bash_refs,
        "totals": {"invoke": ti, "file_ops": tf, "reads": tr, "writes": tw, "bash_refs": tb, "total": ti + tf + tb},
    }


def count_memory_activity(path: str | Path) -> dict:
    """Count per-memory read/write/edit counts (not deduplicated)."""
    p = Path(path)
    if not p.exists():
        return {"per_memory_reads": {}, "per_memory_writes": {}, "per_memory_edits": {}}
    reads: dict[str, int] = {}
    writes: dict[str, int] = {}
    edits: dict[str, int] = {}
    for d in _iter_lines(p):
        if d.get("type") != "assistant":
            continue
        content = (d.get("message") or {}).get("content", [])
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict) or c.get("type") != "tool_use":
                continue
            tool_name = c.get("name", "")
            if tool_name not in ("Read", "Write", "Edit"):
                continue
            inp = c.get("input") or {}
            fp = str(inp.get("file_path", ""))
            if "/memory/" not in fp:
                continue
            mem_name = fp.rsplit("/", 1)[-1].replace(".md", "")
            if mem_name == "MEMORY":
                continue
            if tool_name == "Read":
                reads[mem_name] = reads.get(mem_name, 0) + 1
            elif tool_name == "Write":
                writes[mem_name] = writes.get(mem_name, 0) + 1
            elif tool_name == "Edit":
                edits[mem_name] = edits.get(mem_name, 0) + 1
    return {"per_memory_reads": reads, "per_memory_writes": writes, "per_memory_edits": edits}


def extract_memory_ops(path: str | Path) -> list[dict]:
    """Extract unique memory file operations: [{name, operation, content_preview?}]."""
    p = Path(path)
    if not p.exists():
        return []
    ops: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for d in _iter_lines(p):
        if d.get("type") != "assistant":
            continue
        content = (d.get("message") or {}).get("content", [])
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict) or c.get("type") != "tool_use":
                continue
            tool_name = c.get("name", "")
            if tool_name not in ("Read", "Write", "Edit"):
                continue
            inp = c.get("input") or {}
            file_path = str(inp.get("file_path", ""))
            if "/memory/" not in file_path:
                continue
            mem_name = file_path.rsplit("/", 1)[-1].replace(".md", "")
            if mem_name == "MEMORY":
                continue
            op = "read" if tool_name == "Read" else tool_name.lower()
            key = (mem_name, op)
            if key not in seen:
                seen.add(key)
                entry: dict = {"name": mem_name, "operation": op}
                if tool_name == "Write":
                    entry["content_preview"] = (inp.get("content") or "")[:300]
                elif tool_name == "Edit":
                    # Same reason as the timeline card: a raw before/after pair
                    # spends the preview on the prefix both sides share.
                    entry["content_preview"] = edit_diff(
                        inp.get("old_string"), inp.get("new_string"), 300,
                    )
                ops.append(entry)
    return ops


def extract_background_tasks(path: str | Path) -> list[dict]:
    """Extract ACTIVE (unresolved) background Bash/Monitor tasks."""
    p = Path(path)
    if not p.exists():
        return []
    bg_by_id: dict[str, dict] = {}
    resolved_ids: set[str] = set()
    for d in _iter_lines(p):
        if d.get("type") == "assistant":
            for c in ((d.get("message") or {}).get("content") or []):
                if not isinstance(c, dict) or c.get("type") != "tool_use":
                    continue
                name = c.get("name", "")
                inp = c.get("input") or {}
                tid = c.get("id", "")
                if name == "Bash" and inp.get("run_in_background") and tid:
                    bg_by_id[tid] = {
                        "type": "bash_bg",
                        "description": (inp.get("description") or "")[:200],
                        "command": (inp.get("command") or "")[:200],
                    }
                elif name == "Monitor" and inp.get("persistent") and tid:
                    bg_by_id[tid] = {
                        "type": "monitor",
                        "description": (inp.get("description") or "")[:200],
                        "command": (inp.get("command") or "")[:200],
                    }
        elif d.get("type") == "user":
            for c in ((d.get("message") or {}).get("content") or []):
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    resolved_ids.add(c.get("tool_use_id", ""))
    return [t for tid, t in bg_by_id.items() if tid not in resolved_ids]


def extract_plan_history(path: str | Path) -> list[dict]:
    """Extract chronological plan file mutations from a transcript.

    Returns [{ts, plan_file, operation, version_label, content, diff}].
    Write = full content snapshot. Edit = old_string/new_string diff.
    """
    p = Path(path)
    if not p.exists():
        return []
    history: list[dict] = []
    write_count: dict[str, int] = {}
    edit_count: dict[str, int] = {}
    for d in _iter_lines(p):
        if d.get("type") != "assistant":
            continue
        ts = ""
        msg = d.get("message") or {}
        if "timestamp" not in msg and d.get("timestamp"):
            ts = d["timestamp"]
        else:
            ts = msg.get("timestamp", "")
        content_list = msg.get("content", [])
        if not isinstance(content_list, list):
            continue
        for c in content_list:
            if not isinstance(c, dict) or c.get("type") != "tool_use":
                continue
            tool_name = c.get("name", "")
            if tool_name not in ("Write", "Edit"):
                continue
            inp = c.get("input") or {}
            fp = str(inp.get("file_path", ""))
            if "/.claude/plans/" not in fp or not fp.endswith(".md"):
                continue
            plan_name = fp.rsplit("/", 1)[-1]
            if tool_name == "Write":
                write_count[plan_name] = write_count.get(plan_name, 0) + 1
                edit_count[plan_name] = 0
                vn = write_count[plan_name]
                history.append({
                    "ts": ts,
                    "plan_file": plan_name,
                    "operation": "write",
                    "version_label": f"v{vn}",
                    "content": inp.get("content", ""),
                    "diff": None,
                })
            elif tool_name == "Edit":
                vn = write_count.get(plan_name, 0)
                edit_count[plan_name] = edit_count.get(plan_name, 0) + 1
                en = edit_count[plan_name]
                old_s = inp.get("old_string", "")
                new_s = inp.get("new_string", "")
                history.append({
                    "ts": ts,
                    "plan_file": plan_name,
                    "operation": "edit",
                    "version_label": f"v{vn}.{en}",
                    "content": None,
                    "diff": {"old": old_s[:2000], "new": new_s[:2000]},
                })
    return history

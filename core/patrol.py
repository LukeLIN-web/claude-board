"""Triage classifier: inspect each session's transcript to determine its state."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from .transcripts import _tail_lines, memo_by_file

IDLE_THRESHOLD = 300     # 5 min
CLOSEABLE_THRESHOLD = 3600  # 1 hour
# How long a finished task's notification may sit undelivered before the card
# calls it stuck. Delivery normally takes milliseconds (the notification is
# enqueued and removed in the same tenth of a second), so this is not a tuned
# threshold — it is wide enough that a snapshot can never catch a healthy
# handover mid-flight and flash the card amber.
DELIVERY_GRACE = 60
# Where a reason names how long the card has been idle. The time itself is not
# written in: it grows every second, and a reason that changes by itself would
# have the board re-send every quiet card once a minute (see _TICKING_CARD in
# app.py). The page puts it in from the card's idle_seconds, advanced on its own
# clock, the same way it ticks the card's "3m ago" (cardText in index.html).
IDLE = "{idle}"

TRIAGE_PRIORITY = {
    "waiting_perm": 0,
    "stalled": 1,
    "completed": 2,
    "working": 3,
    "closeable": 4,
}


def _triage(triage: str, reason: str, suggestion: str = "") -> dict:
    """A card's triage fields."""
    return {"triage": triage, "triage_reason": reason, "triage_suggestion": suggestion}


@memo_by_file
def _last_assistant_info(transcript_path: str) -> Optional[dict]:
    """Extract stop_reason, the last tool called and the last thing said in the
    last assistant turn.

    Whether background work is in flight is NOT decided here. It used to be, two
    ways, and both were guesses: any `queue-operation` row after the last
    end_turn counted as work in progress — including `remove`, the row that says
    the queue drained — and failing that, a keyword regex over the assistant's
    last message, so a session that merely mentioned "后台" read as busy. The
    answer comes from unresolved tool calls instead (transcripts.
    extract_background_tasks), which is where it was already computed."""
    p = Path(transcript_path)
    if not p.exists():
        return None

    # Find the last assistant message for stop_reason etc.
    stop_reason = ""
    last_text = ""
    last_tool = ""
    for d in reversed(_tail_lines(p, 40)):
        if d.get("type") != "assistant":
            continue
        msg = d.get("message") or {}
        content = msg.get("content") or []
        stop_reason = msg.get("stop_reason", "")
        if isinstance(content, list) and content:
            if content[-1].get("type") == "tool_use":
                last_tool = content[-1].get("name", "")
            last_text = next((c["text"].strip() for c in reversed(content)
                              if c.get("type") == "text" and c.get("text", "").strip()), "")
        break

    return {
        "stop_reason": stop_reason,
        "last_text": last_text[:200],
        "last_tool": last_tool,
    }


def classify(window_dict: dict) -> dict:
    """Classify a window dict (from sessions.snapshot) into a triage state.

    Returns the card's triage/triage_reason/triage_suggestion fields.
    """
    status = window_dict.get("status", "unknown")
    idle = window_dict.get("idle_seconds", 0)
    transcript = window_dict.get("transcript_path")

    if status == "waiting":
        return _triage("waiting_perm", window_dict.get("waiting_for") or "等待授权", "去终端批准")

    # Ahead of the busy shortcut, because that is what hid this: a session
    # holding a finished task's undelivered notification keeps reporting itself
    # busy, so the card read "working, nothing to do" for as long as it sat
    # there. Work that is done and unread needs a person, not patience.
    stuck = [t for t in (window_dict.get("background_tasks") or [])
             if t.get("state") == "undelivered"
             and time.time() - (t.get("ts") or 0) >= DELIVERY_GRACE]
    if stuck:
        return _triage("stalled", f"后台任务已完成但通知没被取走{_count(stuck)}。{_what(stuck[0])}",
                       "去终端敲一下")

    if status == "busy" and idle < IDLE_THRESHOLD:
        return _triage("working", "正在工作")

    if status == "shell":
        return _triage("working", "shell 进程运行中")

    if not transcript:
        return _triage("closeable", "无 transcript 记录", "可以关闭")

    # Async work still out: a backgrounded Bash, a persistent Monitor, a subagent.
    # app.py fills this in before classifying (transcripts.extract_background_tasks).
    # Ahead of the transcript read below, which this answer doesn't need.
    background = window_dict.get("background_tasks") or []
    if background:
        return _triage("working", f"有后台任务在执行{_count(background)}。{_what(background[0])}")

    info = _last_assistant_info(transcript)
    if not info:
        return _triage("closeable", "transcript 为空", "可以关闭")

    stop = info["stop_reason"]

    if stop == "end_turn":
        summary = info["last_text"].split("\n")[0][:80]
        if idle >= CLOSEABLE_THRESHOLD:
            return _triage("closeable", f"已完成，空闲 {IDLE}。{summary}", "可以关闭")
        return _triage("completed", f"已完成，空闲 {IDLE}。{summary}", "建议 review")

    if stop == "tool_use":
        tool = info["last_tool"]
        if status == "busy":
            return _triage("working", f"正在执行 {tool}" if tool else "正在工作")
        return _triage("stalled", f"停在 {tool}，空闲 {IDLE}" if tool else f"中途停止，空闲 {IDLE}",
                       "需要用户介入")

    # Fallback
    if idle >= CLOSEABLE_THRESHOLD:
        return _triage("closeable", f"空闲 {IDLE}", "可以关闭")
    return _triage("completed" if idle >= IDLE_THRESHOLD else "working", f"空闲 {IDLE}")


def classify_idle(status: str, idle: int, task: str) -> dict:
    """Triage for a card with no Claude transcript to read (Codex, hmz): busy is
    working, otherwise the idle time decides, with `task` — what the session is
    on — after the reason. Returns the card's triage fields, as classify does."""
    if status == "busy":
        return _triage("working", "正在工作")
    tail = f"。{task}" if task else ""
    if idle >= CLOSEABLE_THRESHOLD:
        return _triage("closeable", f"空闲 {IDLE}{tail}", "可以关闭")
    if idle >= IDLE_THRESHOLD:
        return _triage("completed", f"已完成，空闲 {IDLE}{tail}", "建议 review")
    return _triage("completed", f"空闲 {IDLE}{tail}")


def _count(tasks: list) -> str:
    """"（N 件）" when there is more than one; the reason names only the first."""
    return f"（{len(tasks)} 件）" if len(tasks) > 1 else ""


def _what(task: dict) -> str:
    """One line naming a background task, for the card's reason."""
    return (task.get("description") or task.get("command") or "").split("\n")[0][:80]

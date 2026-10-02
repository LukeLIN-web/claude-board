"""Triage classifier: inspect each session's transcript to determine its state."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

IDLE_THRESHOLD = 300     # 5 min
CLOSEABLE_THRESHOLD = 3600  # 1 hour
# How long a finished task's notification may sit undelivered before the card
# calls it stuck. Delivery normally takes milliseconds (the notification is
# enqueued and removed in the same tenth of a second), so this is not a tuned
# threshold — it is wide enough that a snapshot can never catch a healthy
# handover mid-flight and flash the card amber.
DELIVERY_GRACE = 60

TRIAGE_PRIORITY = {
    "waiting_perm": 0,
    "stalled": 1,
    "completed": 2,
    "working": 3,
    "closeable": 4,
}


def _last_assistant_info(transcript_path: str) -> Optional[dict]:
    """Extract stop_reason and the last content block of the last assistant turn.

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
    lines: list[str] = []
    try:
        with p.open() as f:
            for line in f:
                lines.append(line)
    except Exception:
        return None

    # Find the last assistant message for stop_reason etc.
    stop_reason = ""
    last_block_type = ""
    last_text = ""
    last_tool = ""
    for raw in reversed(lines[-40:]):
        try:
            d = json.loads(raw)
        except Exception:
            continue
        if d.get("type") != "assistant":
            continue
        msg = d.get("message") or {}
        content = msg.get("content") or []
        stop_reason = msg.get("stop_reason", "")
        if isinstance(content, list) and content:
            last_block = content[-1]
            last_block_type = last_block.get("type", "")
            if last_block_type == "text":
                last_text = last_block.get("text", "")
            elif last_block_type == "tool_use":
                last_tool = last_block.get("name", "")
            for c in reversed(content):
                if c.get("type") == "text" and c.get("text", "").strip():
                    last_text = c["text"].strip()
                    break
        break

    return {
        "stop_reason": stop_reason,
        "last_block_type": last_block_type,
        "last_text": last_text[:200],
        "last_tool": last_tool,
    }


def classify(window_dict: dict) -> dict:
    """Classify a window dict (from sessions.snapshot) into a triage state.

    Returns {triage, reason, suggestion}.
    """
    status = window_dict.get("status", "unknown")
    idle = window_dict.get("idle_seconds", 0)
    name = window_dict.get("name") or window_dict.get("project_name") or ""
    transcript = window_dict.get("transcript_path")

    if status == "waiting":
        return {
            "triage": "waiting_perm",
            "reason": window_dict.get("waiting_for") or "等待授权",
            "suggestion": "去终端批准",
        }

    # Ahead of the busy shortcut, because that is what hid this: a session
    # holding a finished task's undelivered notification keeps reporting itself
    # busy, so the card read "working, nothing to do" for as long as it sat
    # there. Work that is done and unread needs a person, not patience.
    stuck = [t for t in (window_dict.get("background_tasks") or [])
             if t.get("state") == "undelivered"
             and time.time() - (t.get("ts") or 0) >= DELIVERY_GRACE]
    if stuck:
        return {
            "triage": "stalled",
            "reason": f"后台任务已完成但通知没被取走{_count(stuck)}。{_what(stuck[0])}",
            "suggestion": "去终端敲一下",
        }

    if status == "busy" and idle < IDLE_THRESHOLD:
        return {
            "triage": "working",
            "reason": "正在工作",
            "suggestion": "",
        }

    if status == "shell":
        return {
            "triage": "working",
            "reason": "shell 进程运行中",
            "suggestion": "",
        }

    if not transcript:
        return {
            "triage": "closeable",
            "reason": "无 transcript 记录",
            "suggestion": "可以关闭",
        }

    info = _last_assistant_info(transcript)
    if not info:
        return {
            "triage": "closeable",
            "reason": "transcript 为空",
            "suggestion": "可以关闭",
        }

    stop = info["stop_reason"]
    idle_str = _format_idle(idle)

    # Async work still out: a backgrounded Bash, a persistent Monitor, a subagent.
    # app.py fills this in before classifying (transcripts.extract_background_tasks).
    background = window_dict.get("background_tasks") or []
    if background:
        return {
            "triage": "working",
            "reason": f"有后台任务在执行{_count(background)}。{_what(background[0])}",
            "suggestion": "",
        }

    if stop == "end_turn":
        summary = info["last_text"].split("\n")[0][:80] if info["last_text"] else ""
        if idle >= CLOSEABLE_THRESHOLD:
            return {
                "triage": "closeable",
                "reason": f"已完成，空闲 {idle_str}。{summary}",
                "suggestion": "可以关闭",
            }
        return {
            "triage": "completed",
            "reason": f"已完成，空闲 {idle_str}。{summary}",
            "suggestion": "建议 review",
        }

    if stop == "tool_use":
        tool = info["last_tool"]
        if status == "busy":
            return {
                "triage": "working",
                "reason": f"正在执行 {tool}" if tool else "正在工作",
                "suggestion": "",
            }
        return {
            "triage": "stalled",
            "reason": f"停在 {tool}，空闲 {idle_str}" if tool else f"中途停止，空闲 {idle_str}",
            "suggestion": "需要用户介入",
        }

    # Fallback
    if idle >= CLOSEABLE_THRESHOLD:
        return {
            "triage": "closeable",
            "reason": f"空闲 {idle_str}",
            "suggestion": "可以关闭",
        }
    return {
        "triage": "completed" if idle >= IDLE_THRESHOLD else "working",
        "reason": f"空闲 {idle_str}",
        "suggestion": "",
    }


def _count(tasks: list) -> str:
    """"（N 件）" when there is more than one; the reason names only the first."""
    return f"（{len(tasks)} 件）" if len(tasks) > 1 else ""


def _what(task: dict) -> str:
    """One line naming a background task, for the card's reason."""
    return (task.get("description") or task.get("command") or "").split("\n")[0][:80]


def _format_idle(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h{m}m" if m else f"{h}h"

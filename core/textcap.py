"""Char caps for the text the board renders in a timeline.

An open timeline is re-fetched every couple of seconds, so raw transcript text
can't go over the wire whole — one pasted build log would dominate every poll.
Cap it, but never silently: `cap_text` appends a marker naming how much it
dropped, so a cut-off message reads as cut off instead of as a finished thought.
"""
from __future__ import annotations

import os


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ[name]))
    except (KeyError, ValueError):
        return default


# User/assistant messages — generous, since reading them is the point of the
# timeline. Only a pasted file or a giant log should ever reach this.
MESSAGE_CHARS = _env_int("CLAUDE_FLEET_MESSAGE_CHARS", 20000)
# Tool output still stays shorter than a message — a board left open on a screen
# shouldn't dump whole stdout, and one grep hit can run to 60k chars. Enough to
# read the result, not enough to drown the poll; the timeline collapses it to a
# few lines until clicked. CLAUDE_FLEET_TOOL_RESULT_CHARS=200 restores the old cap.
TOOL_RESULT_CHARS = _env_int("CLAUDE_FLEET_TOOL_RESULT_CHARS", 2000)
# One tool_use argument value (a call shows up to 6 of them).
TOOL_ARG_CHARS = _env_int("CLAUDE_FLEET_TOOL_ARG_CHARS", 600)


def cap_text(text: str | None, limit: int) -> str:
    """`text` cut to `limit` chars, marked with how much was dropped."""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（还有 {len(text) - limit} 字未显示）"

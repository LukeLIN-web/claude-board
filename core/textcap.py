"""Char caps for the text the board renders in a timeline.

An open timeline is re-fetched every couple of seconds, so raw transcript text
can't go over the wire whole — one pasted build log would dominate every poll.
Cap it, but never silently: `cap_text` appends a marker naming how much it
dropped, so a cut-off message reads as cut off instead of as a finished thought.

`edit_diff` spends that budget better for an Edit's before/after pair: it sends
the diff of the two, not the two, so the cap buys the change itself rather than
the identical prefix leading up to it.
"""
from __future__ import annotations

import os
from difflib import SequenceMatcher


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ[name]))
    except (KeyError, ValueError):
        return default


# User/assistant messages — generous, since reading them is the point of the
# timeline. Only a pasted file or a giant log should ever reach this.
MESSAGE_CHARS = _env_int("CLAUDE_FLEET_MESSAGE_CHARS", 20000)
# Tool output stays short on purpose: a board left open on a screen shouldn't
# dump whole stdout, and one grep hit can run to 60k chars on a timeline that
# re-polls every couple of seconds. Raise CLAUDE_FLEET_TOOL_RESULT_CHARS to read
# results in full — the marker below already says how much is being held back.
TOOL_RESULT_CHARS = _env_int("CLAUDE_FLEET_TOOL_RESULT_CHARS", 200)
# One tool_use argument value (a call shows up to 6 of them).
TOOL_ARG_CHARS = _env_int("CLAUDE_FLEET_TOOL_ARG_CHARS", 200)
# Harness-injected user rows (`isMeta`): a skill's whole SKILL.md body, the
# local-command caveat, an image placeholder. Nobody typed these, and a skill
# body runs to 150k chars — as a `user_text` row it buries the turn that invoked
# it. Keep a one-line trace (the marker names the rest) instead of the wall.
META_CHARS = _env_int("CLAUDE_FLEET_META_CHARS", 200)


# The goal sentence pinned above the timeline. One sentence out of a recap, and
# recaps top out around 300 chars — this only guards against a model that writes
# a paragraph without a full stop, since the banner never scrolls away.
GOAL_CHARS = _env_int("CLAUDE_FLEET_GOAL_CHARS", 300)

# The looping prompt pinned next to the goal. A /loop task is something someone
# typed on one line, so this is generous for it — the cap only exists because a
# scheduled prompt can be pasted rather than typed, and the banner never scrolls.
LOOP_CHARS = _env_int("CLAUDE_FLEET_LOOP_CHARS", 300)


# One Edit rendered as a - / + diff. Bigger than a plain tool arg, since this is
# the whole point of the row and the shared text has already been dropped.
EDIT_DIFF_CHARS = _env_int("CLAUDE_FLEET_EDIT_DIFF_CHARS", 400)
# Inside a single changed line: below this many shared chars the agreement is
# context worth reading, not noise — and the marker that would replace it costs
# about as much as the text itself.
ELIDE_MIN_CHARS = _env_int("CLAUDE_FLEET_ELIDE_MIN_CHARS", 40)
# Shared chars kept on each side of a mid-line change, so a one-word edit still
# shows what it sits in.
ELIDE_CONTEXT_CHARS = 24
# An unchanged run this short is cheaper to show than to describe.
CONTEXT_LINES = 2
# Past this many changed lines, skip the matcher (it is O(n·m) and the timeline
# re-runs on every poll) and just list the two sides.
MAX_DIFF_LINES = 400


def cap_text(text: str | None, limit: int) -> str:
    """`text` cut to `limit` chars, marked with how much was dropped."""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（还有 {len(text) - limit} 字未显示）"


def _shared_run(a: list, b: list) -> tuple[int, int]:
    """(leading, trailing) count of items `a` and `b` agree on, non-overlapping."""
    n = min(len(a), len(b))
    head = 0
    while head < n and a[head] == b[head]:
        head += 1
    tail = 0
    while tail < n - head and a[len(a) - 1 - tail] == b[len(b) - 1 - tail]:
        tail += 1
    return head, tail


def trim_shared(old: str, new: str) -> tuple[str, str]:
    """One changed line's before/after, with the chars they agree on dropped.

    A rewritten line usually differs by a word. Cutting the shared head and tail
    (a little of it kept as context) leaves the word instead of a pair of
    near-identical lines the reader has to compare by eye.
    """
    head, tail = _shared_run(list(old), list(new))
    drop_head = max(0, head - ELIDE_CONTEXT_CHARS)
    drop_tail = max(0, tail - ELIDE_CONTEXT_CHARS)
    if drop_head + drop_tail < ELIDE_MIN_CHARS:
        return old, new  # a short agreement is context, not noise
    pre = f"…（前 {drop_head} 字相同）" if drop_head else ""
    post = f"…（后 {drop_tail} 字相同）" if drop_tail else ""
    return (
        pre + old[drop_head:len(old) - drop_tail] + post,
        pre + new[drop_head:len(new) - drop_tail] + post,
    )


def _emit_equal(out: list[str], run: list[str]) -> None:
    """An unchanged run: shown if it's short, named if it isn't."""
    if not run:
        return
    if len(run) <= CONTEXT_LINES and sum(len(x) for x in run) <= 120:
        out.extend("  " + x for x in run)
    else:
        out.append(f"…（{len(run)} 行相同）")


def edit_diff(old: str | None, new: str | None, limit: int = EDIT_DIFF_CHARS) -> str:
    """An Edit as one `-`/`+` diff, with the text both sides share collapsed.

    An Edit sends two near-identical strings. Shown as two capped blocks they're
    unreadable twice over: the cap is spent on the identical prefix, and finding
    the change means diffing the two blocks by eye. So diff them here and send
    only the result — deleted lines under `-`, added under `+`, an unchanged run
    as `…（N 行相同）`, never silently dropped.
    """
    old, new = old or "", new or ""
    a, b = old.splitlines(), new.splitlines()
    head, tail = _shared_run(a, b)
    mid_a, mid_b = a[head:len(a) - tail], b[head:len(b) - tail]

    lines: list[str] = []
    _emit_equal(lines, a[:head])
    if len(mid_a) + len(mid_b) > MAX_DIFF_LINES:
        lines.extend("- " + x for x in mid_a)
        lines.extend("+ " + x for x in mid_b)
    else:
        for tag, i1, i2, j1, j2 in SequenceMatcher(None, mid_a, mid_b, autojunk=False).get_opcodes():
            if tag == "equal":
                _emit_equal(lines, mid_a[i1:i2])
            elif tag == "replace" and i2 - i1 == 1 and j2 - j1 == 1:
                o, n = trim_shared(mid_a[i1], mid_b[j1])  # one line rewritten
                lines.extend(("- " + o, "+ " + n))
            else:
                lines.extend("- " + x for x in mid_a[i1:i2])
                lines.extend("+ " + x for x in mid_b[j1:j2])
    _emit_equal(lines, a[len(a) - tail:] if tail else [])

    if old != new and not any(ln[:1] in "-+" for ln in lines):
        lines.append("…（仅换行/行尾空白不同）")  # splitlines() hides those
    return cap_text("\n".join(lines), limit)

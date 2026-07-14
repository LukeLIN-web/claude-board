"""Reliable tracking of prompts the dashboard has sent but Claude hasn't read.

claude-fleet injects prompts straight into the Claude TUI pane; while a session
is busy they pile up in Claude's own queue. Because *we* are the sender we can
track dashboard-sent prompts exactly, then drop each one once it surfaces in the
transcript (Claude picked it up) or when the session goes idle (queue drained).

This is the reliable half of the card's "Queued (N)" list; the best-effort half
(prompts typed directly in the TUI) is scraped from the pane in core.actions.
"""
from __future__ import annotations

import itertools
import time
from typing import Optional

from . import transcripts

# pid -> [{id, norm, text, ts}]  (ts = epoch seconds when we sent it)
_sent: dict[int, list[dict]] = {}
_ids = itertools.count(1)


def _bare_command(text: str) -> str:
    """Drop a single leading slash so a "/cmd" send matches the transcript.

    Claude logs a slash command as a `<command-name>` envelope that
    transcripts._clean_command_text renders as a bare label (no leading slash).
    record_sent stores the raw "/cmd", so without shedding the slash here the two
    never reconcile and the command sticks in the queue until the session idles.
    Applied symmetrically (both the send and the transcript side run through
    norm), so non-command prompts that merely start with "/" still match.
    """
    return text[1:] if text.startswith("/") else text


def norm(text: str) -> str:
    """Whitespace-collapsed, slash-command-bare form, for matching our send
    against the transcript (which stores slash commands as bare labels)."""
    return " ".join(_bare_command((text or "").strip()).split())


def record_sent(pid: int, text: str, ts: Optional[float] = None) -> None:
    """Record a prompt sent from the dashboard to `pid`'s session.

    `ts` must be sampled BEFORE the keystrokes go out, not after the send call
    returns: typing + composer/submit verification take a few hundred ms, and
    Claude timestamps its transcript row the moment it receives the prompt. Stamp
    it afterwards and that row lands *earlier* than the send time, so the `ts >=`
    guard in `pending` rejects the real match and the prompt sticks on the card.
    """
    n = norm(text)
    if not n:
        return
    # `norm` is the slash-insensitive match key; `text` keeps the "/" the user
    # typed so the card label still reads "/btw", not "btw".
    display = " ".join((text or "").split())
    _sent.setdefault(pid, []).append(
        {"id": next(_ids), "norm": n, "text": display,
         "ts": time.time() if ts is None else ts}
    )


def clear(pid: int) -> None:
    _sent.pop(pid, None)


def pending(pid: int, transcript_path: Optional[str], status: str) -> list[dict]:
    """Tracked prompts Claude hasn't processed yet, as [{id, text}] (send order).

    Reconciliation:
      - status == "idle": the queue can't outlive an idle session -> clear all.
      - else: drop one tracked item per matching consumed-prompt row (a user turn,
        or Claude pulling the prompt off its own queue -- see
        transcripts.consumed_prompt_texts) whose timestamp is at/after the send,
        so an older identical prompt can't falsely consume a freshly queued one.
        Duplicates clear in send order.
      - then the FIFO sweep below.
    """
    items = _sent.get(pid)
    if not items:
        return []
    if status == "idle":
        clear(pid)
        return []

    if transcript_path:
        oldest = min(it["ts"] for it in items)
        seen = transcripts.consumed_prompt_texts(transcript_path, since=oldest)
        remaining: list[dict] = []
        newest_consumed = 0.0
        # Greedy match in send order: each transcript hit consumes one item.
        for it in items:
            hit = next(
                (k for k, (ts, txt) in enumerate(seen)
                 if ts >= it["ts"] and norm(txt) == it["norm"]),
                None,
            )
            if hit is None:
                remaining.append(it)
            else:
                seen.pop(hit)  # don't let one message clear two queued copies
                newest_consumed = max(newest_consumed, it["ts"])
        # FIFO sweep: Claude drains its queue in send order, so once a prompt is
        # consumed, every prompt sent before it is settled -- an unmatched one is
        # gone for good (swallowed by an overlay, or its rows went to a transcript
        # that /clear or a compact rotated away). Keeping it would strand it on the
        # card until the session next idles, which is the bug users see.
        items = [it for it in remaining if it["ts"] >= newest_consumed]
        _sent[pid] = items

    return [{"id": it["id"], "text": it["text"]} for it in items]

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


def norm(text: str) -> str:
    """Whitespace-collapsed form, for matching our send against the transcript."""
    return " ".join((text or "").split())


def record_sent(pid: int, text: str) -> None:
    """Record a prompt sent from the dashboard to `pid`'s session."""
    n = norm(text)
    if not n:
        return
    _sent.setdefault(pid, []).append(
        {"id": next(_ids), "norm": n, "text": n, "ts": time.time()}
    )


def clear(pid: int) -> None:
    _sent.pop(pid, None)


def pending(pid: int, transcript_path: Optional[str], status: str) -> list[dict]:
    """Tracked prompts Claude hasn't processed yet, as [{id, text}] (send order).

    Reconciliation:
      - status == "idle": the queue can't outlive an idle session -> clear all.
      - else: drop one tracked item per matching transcript user message whose
        timestamp is at/after the send (so an older identical prompt can't
        falsely consume a freshly queued one). Duplicates clear in send order.
    """
    items = _sent.get(pid)
    if not items:
        return []
    if status == "idle":
        clear(pid)
        return []

    if transcript_path:
        seen = transcripts.recent_user_texts(transcript_path)
        remaining: list[dict] = []
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
        items = remaining
        _sent[pid] = items

    return [{"id": it["id"], "text": it["text"]} for it in items]

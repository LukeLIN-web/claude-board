"""Who sent each line the board typed into a session.

A line typed into a pane reaches the transcript as a user row like any other, so a
transcript cannot tell a person at the page from a script calling the API, nor either
from somebody typing at the terminal. The board is the one sender that knows, and says so
here: every line it sends is logged with `by`, and a timeline's user rows that match one
carry it (`extra.by`). A row with none was typed at the terminal.

`by` is what the sender named itself (the prompt body's `by`), else `api` for a request
carrying the bearer token and `web` for the page. A board forwarding a card's prompt to a
peer names it first, so the peer logs who sent it, not the board that passed it on.

One JSONL per session under ~/.claude/fleet_sentlog, the place of the board's other own
logs (btwlog); a disk failure only loses the label.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from .promptqueue import norm
from .transcripts import _parse_ts

_DIR = Path.home() / ".claude" / "fleet_sentlog"
# A sent line waits in the session's own queue while it is busy, so its row can come
# long after the send; one that never shows within this long is not matched.
_WINDOW_S = 24 * 3600


def _path(session_id: str) -> Path:
    return _DIR / f"{session_id.replace('/', '_')}.jsonl"


def record(session_id: Optional[str], text: str, by: str, ts: Optional[float] = None) -> None:
    """Log that the board sent `text` to `session_id` for `by`. `ts` is taken before the
    keys go out, as promptqueue.record_sent's is."""
    n = norm(text)
    if not session_id or not n:
        return
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
        with _path(session_id).open("a") as f:
            f.write(json.dumps({"ts": time.time() if ts is None else ts, "norm": n, "by": by},
                               ensure_ascii=False) + "\n")
    except OSError:
        pass


def _sent(session_id: str) -> list[dict]:
    try:
        with _path(session_id).open() as f:
            return [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError):
        return []


def label(session_id: Optional[str], events: list[dict]) -> list[dict]:
    """`events` with `extra.by` on each typed user row the board sent: each send matches
    the first row of the same text at or after it, in send order. Labelled rows are
    copies, since the events may be a cache's."""
    sent = sorted(_sent(session_id), key=lambda s: s["ts"]) if session_id else []
    if not sent:
        return events
    out = list(events)
    rows = [i for i, e in enumerate(out)
            if e.get("kind") == "user_text" and not (e.get("extra") or {}).get("meta")]
    for s in sent:
        for i in rows:
            e = out[i]
            if s["ts"] - 2 <= _parse_ts(e.get("ts", "")) <= s["ts"] + _WINDOW_S and norm(e.get("text", "")) == s["norm"]:
                out[i] = {**e, "extra": {**(e.get("extra") or {}), "by": s["by"]}}
                rows.remove(i)
                break
    return out

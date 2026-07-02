"""Disk-persisted archive of /btw aside answers, keyed by session.

A /btw aside is answered in an ephemeral TUI overlay and is *never* written to
the session transcript — verified empirically: a session whose only interaction
is /btw produces no transcript file at all. So the fleet is the only place that
can remember it. We scrape the overlay from the pane (best-effort, only while it
is on-screen; see actions.parse_btw_overlay), latch each distinct Q+A here, and
persist to disk so the answer survives the overlay being dismissed, the pane
scrolling, and the fleet process restarting.

Surfaced two ways: `latest()` for the card, `timeline_events()` merged into the
session timeline. Shape mirrors core.promptqueue (in-memory dict) but adds a
disk backing so the archive is durable — disk is the source of record, the dict
is a warm cache.
"""
from __future__ import annotations

import itertools
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# One JSONL per session under the user's Claude dir; each line is one entry.
_DIR = Path.home() / ".claude" / "fleet_btwlog"
_ids = itertools.count(1)
# session_id -> [{id, ts, question, answer}]  (ts = epoch seconds)
_cache: dict[str, list[dict]] = {}


def _path(session_id: str) -> Path:
    return _DIR / f"{session_id}.jsonl"


def _load(session_id: str) -> list[dict]:
    """Entries for a session, reading the backing file once then caching."""
    cached = _cache.get(session_id)
    if cached is not None:
        return cached
    items: list[dict] = []
    p = _path(session_id)
    if p.exists():
        try:
            for line in p.read_text().splitlines():
                line = line.strip()
                if line:
                    items.append(json.loads(line))
        except Exception:
            items = []  # a corrupt file degrades to empty, never raises
    _cache[session_id] = items
    return items


def record(session_id: str, question: str, answer: str) -> Optional[dict]:
    """Latch one scraped /btw Q+A. Returns the new entry, or None if it duplicated
    the most recent one — the same overlay is re-scraped every 2s, so de-duping
    against the tail keeps a still-open aside from being stored repeatedly."""
    if not session_id or not (answer or "").strip():
        return None
    q = (question or "").strip()
    a = answer.strip()
    items = _load(session_id)
    if items and items[-1].get("question") == q and items[-1].get("answer") == a:
        return None
    entry = {"id": next(_ids), "ts": time.time(), "question": q, "answer": a}
    items.append(entry)
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
        with _path(session_id).open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # disk failure degrades to in-memory only
    return entry


def has_prefix(session_id: str, question: str, answer_prefix: str) -> bool:
    """Is an aside with this question, whose stored answer *starts with*
    `answer_prefix`, already archived? The scroll-stitch capture is top-anchored,
    so the visible top slice is a prefix of the full answer — this lets the gate
    (core.btwcapture) recognise an already-fully-captured aside from its cheap top
    slice and skip re-scraping it (which would re-inject scroll keys)."""
    if not session_id:
        return False
    q = (question or "").strip()
    a = (answer_prefix or "").strip()
    if not a:
        return False
    return any(e.get("question") == q and (e.get("answer") or "").startswith(a)
               for e in _load(session_id))


def latest(session_id: str) -> Optional[dict]:
    """Most recent aside for the card, or None."""
    if not session_id:
        return None
    items = _load(session_id)
    return items[-1] if items else None


def entries(session_id: str) -> list[dict]:
    """All archived asides for a session, oldest first."""
    return list(_load(session_id)) if session_id else []


def clear(session_id: str) -> None:
    """Drop a session's archive (memory + disk). Not called on idle — the archive
    is meant to outlive the session; provided for explicit resets."""
    if not session_id:
        return
    _cache.pop(session_id, None)
    try:
        _path(session_id).unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def _iso(ts: float) -> str:
    """Epoch seconds -> the Z-suffixed ISO-8601 string transcripts use, so merged
    timeline events sort against real turns by timestamp."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def timeline_events(session_id: str) -> list[dict]:
    """Archived asides as synthetic timeline events (same shape as
    transcripts.TurnEvent dicts): a user_text question + assistant_text answer per
    aside, tagged extra.source="btw" so the UI can badge them."""
    out: list[dict] = []
    for e in entries(session_id):
        iso = _iso(e.get("ts", 0.0))
        if e.get("question"):
            out.append({"ts": iso, "kind": "user_text",
                        "text": "/btw " + e["question"], "tool": None,
                        "role": "user", "extra": {"source": "btw"}})
        out.append({"ts": iso, "kind": "assistant_text",
                    "text": e.get("answer", ""), "tool": None,
                    "role": "assistant", "extra": {"source": "btw"}})
    return out

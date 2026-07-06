"""Gate + background runner for full /btw answer capture.

A /btw answer taller than its overlay window only ever shows a slice on screen;
recovering the rest means scrolling the overlay (actions.capture_full_btw_answer),
which injects ↓ keys into the live pane and takes seconds. Neither belongs on the
2 s dashboard-refresh path, so this module:

  1. does the cheap, key-free top-slice scrape first (actions.get_btw_state);
  2. skips entirely if that aside is already fully archived (btwlog.has_prefix) —
     so a still-open overlay is not re-scrolled on every poll;
  3. otherwise runs the slow scroll-stitch on a daemon thread, one at a time per
     session, and latches the full answer to btwlog.

The disk gate (has_prefix) is durable: after the full answer is stored, later
polls see the top slice as a prefix of it and stop, even across a fleet restart.

capture_sync is the blocking variant for callers that are about to DESTROY the
overlay (the next prompt's dismiss-Escape, the dashboard Esc button): once the
overlay closes, an un-archived answer is unrecoverable, so those paths must
latch it before pressing Escape rather than hoping a poll got there first.
"""
from __future__ import annotations

import threading
import time

from . import actions, btwlog

_lock = threading.Lock()
_inflight: set[str] = set()  # session_ids with a scroll-stitch currently running

# How long capture_sync will wait out a background stitch already running for
# the same session before giving up (it archives on completion anyway).
_SYNC_INFLIGHT_WAIT = 15.0
_SYNC_INFLIGHT_POLL = 0.2


def maybe_capture(pid: int, session_id: str) -> str | None:
    """Capture the full /btw answer for `pid` if a new (not-yet-archived) settled
    aside is on the pane. Non-blocking: the actual scroll-stitch runs on a daemon
    thread. Best-effort — any failure leaves whatever is already archived intact.

    Returns the question of an aside whose answer is STILL GENERATING (for the
    card's live "answering…" indicator — nothing to archive yet), else None."""
    if not session_id:
        return None
    try:
        state = actions.get_btw_state(pid)  # cheap, no key injection
    except Exception:
        return None
    if not state:
        return None
    if "pending" in state:
        return state["pending"]
    slice_ov = state["settled"]
    if btwlog.has_prefix(session_id, slice_ov["question"], slice_ov["answer"]):
        return None  # already fully archived — don't re-scroll the overlay
    with _lock:
        if session_id in _inflight:
            return None  # a stitch for this session is already running
        _inflight.add(session_id)
    threading.Thread(target=_worker, args=(pid, session_id), daemon=True).start()
    return None


def capture_sync(pid: int, session_id: str) -> None:
    """Archive the settled aside on `pid`'s pane NOW, blocking until latched.

    For pane-mutating callers about to dismiss the overlay. If a background
    stitch for this session is already scrolling, wait for it instead of racing
    it with a second set of ↓ presses; then re-check the archive before doing
    any work of our own. Falls back to latching the visible top slice when the
    full scroll-stitch fails — a truncated answer beats a vanished one."""
    if not session_id:
        return
    try:
        slice_ov = actions.get_btw_answer(pid)
    except Exception:
        return
    if not slice_ov:
        return
    deadline = time.time() + _SYNC_INFLIGHT_WAIT
    while True:
        if btwlog.has_prefix(session_id, slice_ov["question"], slice_ov["answer"]):
            return  # archived (possibly by the stitch we were waiting out)
        with _lock:
            if session_id not in _inflight:
                _inflight.add(session_id)
                break
        if time.time() >= deadline:
            return  # a stuck stitch owns the pane — don't pile on
        time.sleep(_SYNC_INFLIGHT_POLL)
    try:
        full = None
        try:
            full = actions.capture_full_btw_answer(pid)
        except Exception:
            pass
        got = full or slice_ov
        btwlog.record(session_id, got["question"], got["answer"])
    finally:
        with _lock:
            _inflight.discard(session_id)


def _worker(pid: int, session_id: str) -> None:
    try:
        full = actions.capture_full_btw_answer(pid)
        if full:
            btwlog.record(session_id, full["question"], full["answer"])
    except Exception:
        pass  # scrape/scroll failure degrades to the already-latched top slice
    finally:
        with _lock:
            _inflight.discard(session_id)

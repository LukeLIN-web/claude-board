"""Gate + background runner for full /btw answer capture.

A /btw answer taller than its overlay window only ever shows a slice on screen;
recovering the rest means scrolling the overlay
(btwscreen.capture_full_btw_answer), which injects ↓ keys into the live pane and
takes seconds. Neither belongs on the 2 s dashboard-refresh path, so this
module:

  1. does the cheap, key-free top-slice scrape first (btwscreen.get_btw_state);
  2. skips entirely if that aside is already fully archived (btwlog.has_slice) —
     so a still-open overlay is not re-scrolled on every poll;
  3. otherwise runs the slow scroll-stitch on a daemon thread, one at a time per
     session, and latches the full answer to btwlog.

The disk gate (has_slice) is durable: after the full answer is stored, later
polls see whatever slice of it is on screen as part of the stored answer and
stop, even across a fleet restart and wherever the reader has scrolled to.

capture_sync is the blocking variant for callers that are about to DESTROY the
overlay (the next prompt's dismiss-Escape, the dashboard Esc button): once the
overlay closes, an un-archived answer is unrecoverable, so those paths must
latch it before pressing Escape rather than hoping a poll got there first.
"""
from __future__ import annotations

import threading

from . import btwlog, btwscreen, tmux

_lock = threading.Lock()  # guards _session_locks
# session_id -> held for as long as a scroll-stitch of that session's pane runs
_session_locks: dict[str, threading.Lock] = {}

# How long capture_sync will wait out a background stitch already running for
# the same session before giving up (it archives on completion anyway).
_SYNC_INFLIGHT_WAIT = 15.0


def _session_lock(session_id: str) -> threading.Lock:
    with _lock:
        return _session_locks.setdefault(session_id, threading.Lock())


def _archived(session_id: str, slice_ov: dict) -> bool:
    return btwlog.has_slice(session_id, slice_ov["question"], slice_ov["answer"])


def maybe_capture(tty: str, session_id: str) -> str | None:
    """Capture the full /btw answer on `tty`'s pane if a new (not-yet-archived)
    settled aside is up. Non-blocking: the actual scroll-stitch runs on a daemon
    thread. Best-effort — any failure leaves whatever is already archived intact.

    Returns the question of an aside whose answer is STILL GENERATING (for the
    card's live "answering…" indicator — nothing to archive yet), else None."""
    if not session_id:
        return None
    pane = tmux.pane_for_tty(tty)
    if pane is None:
        return None
    try:
        state = btwscreen.get_btw_state(pane)  # cheap, no key injection
    except Exception:
        return None
    if not state:
        return None
    if "pending" in state:
        return state["pending"]
    if _archived(session_id, state["settled"]):
        return None  # already fully archived — don't re-scroll the overlay
    lock = _session_lock(session_id)
    if not lock.acquire(blocking=False):
        return None  # a stitch for this session is already running
    threading.Thread(target=_stitch, args=(lock, pane, session_id), daemon=True).start()
    return None


def capture_sync(pane: str, session_id: str) -> None:
    """Archive the settled aside on `pane` NOW, blocking until latched.

    For pane-mutating callers about to dismiss the overlay. If a background
    stitch for this session is already scrolling, wait for it instead of racing
    it with a second set of ↓ presses; then re-check the archive before doing
    any work of our own. Falls back to latching the visible top slice when the
    full scroll-stitch fails — a truncated answer beats a vanished one."""
    if not session_id:
        return
    try:
        slice_ov = btwscreen.get_btw_answer(pane)
    except Exception:
        return
    if not slice_ov or _archived(session_id, slice_ov):
        return
    lock = _session_lock(session_id)
    if not lock.acquire(timeout=_SYNC_INFLIGHT_WAIT):
        return  # a stuck stitch owns the pane — don't pile on
    if _archived(session_id, slice_ov):
        lock.release()
        return  # archived by the stitch we were waiting out
    _stitch(lock, pane, session_id, fallback=slice_ov)


def _stitch(lock: threading.Lock, pane: str, session_id: str,
            fallback: dict | None = None) -> None:
    """Scroll-stitch the aside on `pane` and archive it, then release `lock` —
    the session's, which the caller acquired. A failed stitch archives
    `fallback` instead when given; otherwise whatever is already archived
    stands."""
    try:
        try:
            full = btwscreen.capture_full_btw_answer(pane)
        except Exception:
            full = None  # a scrape/scroll failure degrades to the fallback
        got = full or fallback
        if got:
            btwlog.record(session_id, got["question"], got["answer"])
    finally:
        lock.release()

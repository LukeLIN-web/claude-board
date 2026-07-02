"""Gate + background runner for full /btw answer capture.

A /btw answer taller than its overlay window only ever shows a slice on screen;
recovering the rest means scrolling the overlay (actions.capture_full_btw_answer),
which injects ↓ keys into the live pane and takes seconds. Neither belongs on the
2 s dashboard-refresh path, so this module:

  1. does the cheap, key-free top-slice scrape first (actions.get_btw_answer);
  2. skips entirely if that aside is already fully archived (btwlog.has_prefix) —
     so a still-open overlay is not re-scrolled on every poll;
  3. otherwise runs the slow scroll-stitch on a daemon thread, one at a time per
     session, and latches the full answer to btwlog.

The disk gate (has_prefix) is durable: after the full answer is stored, later
polls see the top slice as a prefix of it and stop, even across a fleet restart.
"""
from __future__ import annotations

import threading

from . import actions, btwlog

_lock = threading.Lock()
_inflight: set[str] = set()  # session_ids with a scroll-stitch currently running


def maybe_capture(pid: int, session_id: str) -> None:
    """Capture the full /btw answer for `pid` if a new (not-yet-archived) settled
    aside is on the pane. Non-blocking: the actual scroll-stitch runs on a daemon
    thread. Best-effort — any failure leaves whatever is already archived intact."""
    if not session_id:
        return
    try:
        slice_ov = actions.get_btw_answer(pid)  # cheap, no key injection
    except Exception:
        return
    if not slice_ov:
        return
    if btwlog.has_prefix(session_id, slice_ov["question"], slice_ov["answer"]):
        return  # already fully archived — don't re-scroll the overlay
    with _lock:
        if session_id in _inflight:
            return  # a stitch for this session is already running
        _inflight.add(session_id)
    threading.Thread(target=_worker, args=(pid, session_id), daemon=True).start()


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

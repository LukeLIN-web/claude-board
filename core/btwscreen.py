"""A /btw aside on screen: the overlay its answer shows in, read off a pane.

/btw answers show in an ephemeral overlay and are never written to the
transcript, so the pane is the only place to read one. This module parses that
overlay — settled or still answering — and, for an answer taller than the pane,
scrolls it and stitches the frames back into the whole answer. The send path
(actions) uses it to tell when an overlay covers the composer; core.btwcapture
uses it to latch asides to the archive (core.btwlog).
"""
from __future__ import annotations

import re
import time
from typing import Optional

from . import tmux

# A /btw aside answer renders in a modal overlay: a ▔▔▔ (U+2594) top border, the
# echoed "/btw …" question, the answer body, then a "… Esc to close" footer. The
# ▔ border is distinct from the input box's ─ rule (actions._HRULE_RE), so it
# anchors the top; "Esc to close" anchors the bottom.
_BTW_TOP_RE = re.compile(r"^\s*▔{6,}\s*$")


def overlay_open(text: str) -> bool:
    """True if a /btw overlay — settled or still answering — covers the pane."""
    return _overlay_anchors(text) is not None


def overlay_settled(text: str) -> bool:
    """True if the /btw overlay on the pane holds a finished answer."""
    return _btw_regions(text) is not None


def parse_btw_overlay(text: str) -> Optional[dict]:
    """Extract {question, answer} for the *newest* /btw aside, or None.

    /btw answers show in an ephemeral overlay and are never written to the
    transcript, so scraping the pane is the only way to recover them. The overlay
    is a history carousel: firing several /btw stacks every question, but only the
    current (newest) aside's answer is shown. So we pair the LAST "/btw …" line
    with the answer block beneath it — taking the first would swallow later
    question lines into the answer.

    Only latch a *settled* answer: while Claude is still generating, the answer
    region is just an animated spinner ("✽ Answering…") and the footer lacks the
    "c to copy · f to fork" hints (you can't copy an unfinished answer). Requiring
    "to copy" in the footer gates out mid-generation frames — this also stops the
    animating spinner from defeating the archive's de-dupe.

    An answer taller than the pane pushes the question line off the top; the
    question then comes back "" and only capture_full_btw_answer, which can
    scroll, recovers it (see _overlay_anchors).
    """
    got = _btw_regions(text)
    if got is None:
        return None
    question, answer_lines = got
    answer = "\n".join(answer_lines)[:4000]
    if not answer:
        return None
    return {"question": question, "answer": answer}


def _overlay_anchors(text: str) -> Optional[tuple[list[str], int, Optional[int], int]]:
    """(lines, top, q_idx, foot) anchors of any open /btw overlay, or None.

    The footer ("… Esc to close") is the one anchor always on screen, and it is
    what "an overlay is open" means here — capture_full_btw_answer walks a long
    answer window-by-window off these anchors, at every scroll position.
    Settled-or-not is the caller's judgment (the footer at `foot` carries the
    signal): _btw_regions wants finished answers only, parse_btw_pending wants
    the mid-generation state.

    `q_idx` — the echoed "/btw …" question line — is None when the answer is tall
    enough to push the top of the overlay above the visible pane. Requiring it
    made exactly the longest answers unreadable to us, which are the ones worth
    archiving; callers that need the question sit those out, and the answer region
    falls back to the `top` bound.

    Top anchor: older Claude builds draw a ▔ border above the overlay; current
    builds (v2.1.20x) draw none, so fall back to the composer marker line (the
    overlay renders below the composer while it fits on screen) and finally to
    the capture start. The composer line is only ever the (exclusive) top bound —
    while the aside is open it still shows the just-submitted "/btw …" command
    itself, and must never be mistaken for the overlay's question line."""
    if not text:
        return None
    lines = text.split("\n")
    foot = next((i for i in range(len(lines) - 1, -1, -1)
                 if tmux._BTW_OVERLAY_FOOTER in lines[i]), None)
    if foot is None:
        return None
    top = next((i for i in range(foot - 1, -1, -1)
                if _BTW_TOP_RE.match(lines[i])), None)
    if top is None:
        marker = next((i for i in range(foot - 1, -1, -1)
                       if lines[i].lstrip().startswith(("❯", "›"))), None)
        top = marker if marker is not None else -1
    q_idx = next((i for i in range(foot - 1, top, -1)
                  if lines[i].lstrip().startswith("/btw")), None)
    return lines, top, q_idx, foot


def _overlay_question(line: str) -> str:
    question = line.strip()
    if question.startswith("/btw"):
        question = question[len("/btw"):].strip()
    return question


def _btw_regions(text: str) -> Optional[tuple[str, list[str]]]:
    """The (question, visible-answer-lines) of a *settled* /btw overlay, or None.

    Returns None when there is no settled overlay on the pane (no footer, or
    mid-generation), which is also the signal that the overlay has been
    dismissed. The question is "" when the overlay's question line sits above the
    visible pane — the answer is still worth reading, and it is the whole region
    between the top bound and the footer."""
    got = _overlay_anchors(text)
    if got is None:
        return None
    lines, top, q_idx, foot = got
    if "to copy" not in lines[foot]:
        return None  # answer still generating — don't latch a partial/spinner
    question = "" if q_idx is None else _overlay_question(lines[q_idx])
    body = top if q_idx is None else q_idx
    answer_lines = [ln.strip() for ln in lines[body + 1:foot] if ln.strip()]
    return question, answer_lines


def parse_btw_pending(text: str) -> Optional[str]:
    """Question of a /btw aside whose answer is still generating, or None.

    The card would otherwise show nothing while an aside is answering (asides
    never reach the transcript, and the archive only latches settled answers),
    which reads as "/btw did nothing" and invites an Escape that destroys the
    answer. The mid-generation footer lacks the "c to copy" hint — the same
    signal _btw_regions uses, inverted."""
    got = _overlay_anchors(text)
    if got is None:
        return None
    lines, _top, q_idx, foot = got
    if "to copy" in lines[foot]:
        return None  # settled — parse_btw_overlay territory
    if q_idx is None:
        return None  # the indicator *is* the question; nothing to show without it
    return _overlay_question(lines[q_idx])


def get_btw_state(pane: str) -> Optional[dict]:
    """One-capture view of the /btw overlay on `pane`:
    {"settled": {question, answer}} while a finished answer is up,
    {"pending": question} while the answer is still generating, else None.

    The overlay covers the visible pane, so a plain capture (no scrollback, which
    would pull in stale pre-overlay content) is what we want. None on any miss.
    """
    cap = tmux.capture_pane(pane)
    if not cap["ok"]:
        return None
    ov = parse_btw_overlay(cap["text"])
    if ov:
        return {"settled": ov}
    q = parse_btw_pending(cap["text"])
    return {"pending": q} if q is not None else None


def get_btw_answer(pane: str) -> Optional[dict]:
    """The settled /btw overlay on `pane` as {question, answer}, or None."""
    state = get_btw_state(pane)
    return state["settled"] if state and "settled" in state else None


# The /btw overlay shows a long answer only a sliding window at a time, and the
# un-scrolled remainder is never emitted to the terminal (nor the transcript), so
# a single capture truncates it. To recover the whole answer we scroll the window
# to the bottom and stitch each frame. Empirically (Claude Code v2.1.x, 80x24):
# only ↑/↓ scroll — PgUp/PgDn are no-ops and Ctrl-D/Ctrl-F/Space *dismiss* the
# overlay; ↓ advances a few lines and clamps at the bottom (frame stops changing);
# ↑ clamps at the top. Crucially, once the overlay is gone, arrow keys fall
# through to the composer (history recall), so we re-verify the overlay is present
# before every keystroke and, if it has vanished, stop sending keys at once.
_BTW_SCROLL_MAX = 120          # hard cap on ↓ presses (far beyond any real answer)
_BTW_SCROLL_SETTLE = 0.18      # let the overlay redraw before re-capturing
_BTW_ANSWER_MAX = 20000        # sanity cap on a fully-stitched answer

# A pane captured mid-repaint yields a half-applied frame: the overlay footer's
# "↑/" hint and the composer's ─ rules land spliced into the answer region, and a
# diff-rendered line comes back with leftover cells from the line it replaced
# ("max-pos 36864 / af10" read as "max-posg36864c/paf10"). Such a frame is worse
# than no frame: its lines match neither the accumulator's suffix nor the next
# window's prefix, which is the no-overlap case _stitch_btw refuses. Two defences,
# because one round of ↓ presses is long enough to catch a repaint either way:
# every scroll position is read until two consecutive reads agree, and a position
# that never settles aborts the round rather than being stitched.
_BTW_FRAME_TRIES = 3            # reads per scroll position before calling it unstable
_BTW_FRAME_RECHECK = 0.12       # settle between those reads
_BTW_FRAME_UNSTABLE = object()  # sentinel: pane repainting, no frame worth stitching


def _stable_btw_regions(pane: str) -> tuple[str, list[str]] | None | object:
    """`_btw_regions` for the overlay at the pane's current scroll position, read
    repeatedly until two consecutive reads agree.

    Returns (question, answer_lines) for a settled, stable overlay; None when
    there is no settled overlay (including one that vanished between reads, so the
    caller stops sending keys at once); the _BTW_FRAME_UNSTABLE sentinel when the
    regions kept changing — the pane is repainting and any frame taken from it may
    be a spliced half-redraw.

    Only the overlay's OWN regions are compared, so a busy session's spinner and
    elapsed-time counter ticking elsewhere on the pane never count as instability.
    """
    prev = _btw_regions(tmux.capture_pane(pane).get("text", ""))
    if prev is None:
        return None
    for _ in range(_BTW_FRAME_TRIES - 1):
        time.sleep(_BTW_FRAME_RECHECK)
        cur = _btw_regions(tmux.capture_pane(pane).get("text", ""))
        if cur is None:
            return None
        if cur == prev:
            return cur
        prev = cur
    return _BTW_FRAME_UNSTABLE


def _stitch_btw(acc: list[str], window: list[str]) -> Optional[list[str]]:
    """Append a later, overlapping scroll `window` onto `acc`, dropping the longest
    prefix of `window` that is already the suffix of `acc`. Equal frames (the
    window clamped at the bottom) leave `acc` unchanged — the caller's stop signal.

    None when the two share no overlap at all. ↓ scrolls the answer by a few lines
    inside a window-height view, so consecutive frames ALWAYS overlap: no overlap
    does not mean the answer jumped, it means the frame was read mid-repaint and
    its lines are spliced. Concatenating one anyway is what grew a single aside
    four duplicated copies of itself — and a stitch corrupted that way is no
    longer a prefix of the next poll's top slice, so the gate meant to end the
    retry reopens and archives another mangled copy every time the pane blinks."""
    if not acc:
        return list(window)
    for o in range(min(len(acc), len(window)), 0, -1):
        if acc[-o:] == window[:o]:
            return acc + window[o:]
    return None


def capture_full_btw_answer(pane: str) -> Optional[dict]:
    """The *complete* /btw aside on `pane`, scrolling the overlay to recover an
    answer taller than the visible window. None if no settled overlay is up.

    Injects ↓ keys into the live pane, so callers must gate this (see
    core.btwcapture): only run it for a not-yet-archived aside, off the hot path.
    Returns None rather than a guess when the pane is repainting too hard to read
    a trustworthy frame (see _stable_btw_regions).
    """
    frame = _stable_btw_regions(pane)
    if frame is None or frame is _BTW_FRAME_UNSTABLE:
        return None  # no settled overlay, or an unreadable pane — no keystrokes
    # Walk to the top before stitching. The overlay does not have to be sitting
    # there: the reader scrolls it, and an answer taller than the pane opens with
    # its head already off the top edge. The stitch below only ever goes down, so
    # starting anywhere else drops the head of the answer — and with it the
    # question line, which is what a "" question from _btw_regions means. ↑ clamps
    # at the top and never dismisses the overlay, so this runs the same key
    # discipline as the ↓ walk: re-read after every press, and stop the moment the
    # overlay is gone rather than type into the composer behind it.
    for _ in range(_BTW_SCROLL_MAX):
        tmux.send_keys(pane, "Up")
        time.sleep(_BTW_SCROLL_SETTLE)
        cur = _stable_btw_regions(pane)
        if cur is None or cur is _BTW_FRAME_UNSTABLE:
            return None
        if cur == frame:
            break  # frame stopped changing — clamped at the top
        frame = cur
    question, acc = frame
    presses = 0
    overlay_alive = True
    unstable = False
    while presses < _BTW_SCROLL_MAX:
        tmux.send_keys(pane, "Down")
        presses += 1
        time.sleep(_BTW_SCROLL_SETTLE)
        cur = _stable_btw_regions(pane)
        if cur is None:
            # Overlay dismissed mid-scroll: stop now and DO NOT restore — further
            # arrows would drive the composer's history instead of the overlay.
            overlay_alive = False
            break
        if cur is _BTW_FRAME_UNSTABLE:
            unstable = True
            break  # restore the view below, then abandon the round
        merged = _stitch_btw(acc, cur[1])
        if merged is None:
            unstable = True
            break  # spliced frame — same answer as an unreadable pane
        if merged == acc:
            break  # window clamped at the bottom — whole answer captured
        acc = merged
    if overlay_alive:
        # Restore the user's view to the top: exactly as many ↑ as ↓ (both clamp,
        # so this lands back at the top). ↑ never dismisses the overlay.
        for _ in range(presses):
            tmux.send_keys(pane, "Up")
            time.sleep(_BTW_SCROLL_SETTLE)
    if unstable:
        # Archive nothing: the aside stays un-gated, so a later poll retries and a
        # quieter pane yields a clean stitch. Storing the half-merged `acc` would
        # poison the archive AND the gate that is supposed to end the retry loop.
        return None
    answer = "\n".join(acc)[:_BTW_ANSWER_MAX]
    return {"question": question, "answer": answer} if answer else None

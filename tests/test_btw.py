"""Tests for /btw aside capture: overlay parsing (core.actions.parse_btw_overlay),
the disk-persisted archive (core.btwlog), and the timeline merge (app.api_timeline).
"""
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import app as appmod
from core import actions, btwlog


# Real capture from a live Claude pane: a /btw aside overlay with a multi-line
# answer. Top border is U+2594 (▔), footer is the "… Esc to close" hint.
_BORDER = "▔" * 60
CAP_BTW = f"""\



{_BORDER}

    /btw name the three primary colors, one word per line, nothing else.

      Red

      Yellow

      Blue

    ↑/↓ to scroll · c to copy · f to fork · Esc to close
"""


# Real capture: several /btw fired in a row stack into one history-carousel
# overlay. The question region lists every /btw; the answer region shows only the
# CURRENT (newest) aside's answer. Footer switches to "←/→ to switch" and gains
# "x to clear history"; it keeps "c to copy · f to fork" once the answer settles.
CAP_MULTI = f"""\
{_BORDER}

    /btw what is recall in one sentence?
    /btw name three primary colors, one per line.

      Red
      Blue
      Yellow

    ←/→ to switch · c to copy · f to fork · x to clear history · Esc to close
"""

# Real capture mid-generation: the answer region is just the animated spinner,
# and the footer LACKS "c to copy · f to fork" (you can't copy an unfinished
# answer) — our settled signal.
CAP_MIDGEN = f"""\
{_BORDER}

    /btw what is recall in one sentence?
    /btw name three primary colors, one per line.

      ✽ Answering…

    ←/→ to switch · x to clear history · Esc to close
"""


class ParseBtwOverlayTests(unittest.TestCase):
    def test_multi_aside_takes_newest_qa_only(self):
        # Must pair the LAST /btw question with the shown answer, not swallow the
        # whole history.
        got = actions.parse_btw_overlay(CAP_MULTI)
        self.assertEqual(got["question"], "name three primary colors, one per line.")
        self.assertEqual(got["answer"], "Red\nBlue\nYellow")

    def test_none_while_generating(self):
        # No "c to copy" in the footer => answer not finished => don't latch.
        self.assertIsNone(actions.parse_btw_overlay(CAP_MIDGEN))


    def test_parses_question_and_multiline_answer(self):
        got = actions.parse_btw_overlay(CAP_BTW)
        self.assertEqual(
            got["question"],
            "name the three primary colors, one word per line, nothing else.",
        )
        self.assertEqual(got["answer"], "Red\nYellow\nBlue")

    def test_none_without_footer(self):
        self.assertIsNone(actions.parse_btw_overlay(CAP_BTW.replace("Esc to close", "")))

    def test_none_without_top_border(self):
        # A long answer can scroll the ▔ border off-screen; we prefer a miss to a
        # half-parsed answer.
        self.assertIsNone(actions.parse_btw_overlay(CAP_BTW.replace(_BORDER, "")))

    def test_none_without_btw_line(self):
        self.assertIsNone(actions.parse_btw_overlay(CAP_BTW.replace("/btw ", "")))

    def test_none_when_answer_empty(self):
        cap = f"{_BORDER}\n    /btw hi?\n\n    ↑/↓ to scroll · c to copy · f to fork · Esc to close\n"
        self.assertIsNone(actions.parse_btw_overlay(cap))

    def test_none_on_empty(self):
        self.assertIsNone(actions.parse_btw_overlay(""))


class ParseBtwPendingTests(unittest.TestCase):
    """The mid-generation state: overlay open, footer without "c to copy". The
    card shows this live (there is nothing to archive yet), and the dismiss path
    uses it to wait for the answer instead of destroying it."""

    def test_question_of_generating_aside(self):
        self.assertEqual(
            actions.parse_btw_pending(CAP_MIDGEN),
            "name three primary colors, one per line.",
        )

    def test_none_when_settled(self):
        # A finished answer is parse_btw_overlay's territory, not pending.
        self.assertIsNone(actions.parse_btw_pending(CAP_MULTI))

    def test_none_without_overlay(self):
        self.assertIsNone(actions.parse_btw_pending("some normal pane text\n❯ \n"))


class GetBtwAnswerTests(unittest.TestCase):
    def test_none_when_no_tty(self):
        w = types.SimpleNamespace(tty=None)
        with mock.patch.object(actions, "find_window", return_value=w):
            self.assertIsNone(actions.get_btw_answer(123))

    def test_scrapes_pane(self):
        w = types.SimpleNamespace(tty="/dev/pts/9")
        with mock.patch.object(actions, "find_window", return_value=w), \
             mock.patch.object(actions.tmux, "pane_for_tty", return_value="%3"), \
             mock.patch.object(actions.tmux, "capture_pane",
                               return_value={"ok": True, "text": CAP_BTW}):
            got = actions.get_btw_answer(123)
        self.assertEqual(got["answer"], "Red\nYellow\nBlue")


def _btw_frame(answer_lines, question="print the integers", *, overlay=True, settled=True):
    """Build a captured-pane string mimicking a /btw overlay at some scroll
    position: pinned ▔ border, pinned "/btw …" line, the currently-visible answer
    window, then the pinned footer. overlay=False yields a plain (dismissed) pane
    so parse/extract return None; settled=False drops the "c to copy" hint."""
    if not overlay:
        return "some normal pane text\n❯ \n"
    foot = "↑/↓ to scroll · " + ("c to copy · f to fork · " if settled else "") + "Esc to close"
    body = "\n".join(f"      {ln}" for ln in answer_lines)
    return f"{'▔' * 60}\n\n    /btw {question}\n\n{body}\n\n    {foot}\n"


class StitchBtwTests(unittest.TestCase):
    def test_merges_overlapping_windows(self):
        self.assertEqual(actions._stitch_btw(["a", "b", "c"], ["b", "c", "d"]),
                         ["a", "b", "c", "d"])

    def test_empty_accumulator_takes_new(self):
        self.assertEqual(actions._stitch_btw([], ["a", "b"]), ["a", "b"])

    def test_full_overlap_does_not_grow(self):
        # Bottom reached: the window stopped advancing.
        self.assertEqual(actions._stitch_btw(["a", "b", "c"], ["a", "b", "c"]),
                         ["a", "b", "c"])

    def test_no_overlap_concatenates(self):
        self.assertEqual(actions._stitch_btw(["a", "b"], ["c", "d"]),
                         ["a", "b", "c", "d"])


class CaptureFullBtwAnswerTests(unittest.TestCase):
    def _run(self, frames):
        sent = []
        w = types.SimpleNamespace(tty="/dev/pts/9")
        with mock.patch.object(actions, "find_window", return_value=w), \
             mock.patch.object(actions.tmux, "pane_for_tty", return_value="%3"), \
             mock.patch.object(actions.tmux, "capture_pane",
                               side_effect=[{"ok": True, "text": f} for f in frames]), \
             mock.patch.object(actions.tmux, "send_keys",
                               side_effect=lambda pane, *keys: sent.extend(keys) or {"ok": True}), \
             mock.patch.object(actions.time, "sleep"):
            got = actions.capture_full_btw_answer(123)
        return got, sent

    def test_scrolls_and_stitches_full_answer(self):
        # window of 3 lines, full answer L1..L6, scrolling 1 line per Down until
        # the window clamps at the bottom (last frame == previous).
        frames = [
            _btw_frame(["L1", "L2", "L3"]),
            _btw_frame(["L2", "L3", "L4"]),
            _btw_frame(["L3", "L4", "L5"]),
            _btw_frame(["L4", "L5", "L6"]),
            _btw_frame(["L4", "L5", "L6"]),  # bottom clamp -> stop
        ]
        got, sent = self._run(frames)
        self.assertEqual(got["answer"], "L1\nL2\nL3\nL4\nL5\nL6")
        # 4 Downs advanced the window; the view is then restored with 4 Ups.
        self.assertEqual(sent, ["Down"] * 4 + ["Up"] * 4)

    def test_none_when_no_overlay(self):
        got, sent = self._run([_btw_frame([], overlay=False)])
        self.assertIsNone(got)
        self.assertEqual(sent, [])  # never touch the keyboard without an overlay

    def test_aborts_without_restoring_when_overlay_vanishes(self):
        # If the overlay disappears mid-scroll, stop immediately and send NO Ups —
        # stray arrows would fall through to the composer (history recall).
        frames = [
            _btw_frame(["L1", "L2", "L3"]),
            _btw_frame(["L2", "L3", "L4"]),
            _btw_frame([], overlay=False),  # gone
        ]
        got, sent = self._run(frames)
        self.assertEqual(got["answer"], "L1\nL2\nL3\nL4")
        self.assertEqual(sent, ["Down", "Down"])  # no Up restore


class BtwLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = btwlog._DIR
        btwlog._DIR = Path(self.tmp)
        btwlog._cache.clear()

    def tearDown(self):
        btwlog._DIR = self._orig
        btwlog._cache.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_record_and_latest(self):
        e = btwlog.record("sess1", "q1", "a1")
        self.assertIsNotNone(e)
        self.assertEqual(btwlog.latest("sess1")["answer"], "a1")
        self.assertEqual(len(btwlog.entries("sess1")), 1)

    def test_dedupes_identical_tail(self):
        btwlog.record("sess1", "q1", "a1")
        self.assertIsNone(btwlog.record("sess1", "q1", "a1"))
        self.assertEqual(len(btwlog.entries("sess1")), 1)

    def test_appends_distinct(self):
        btwlog.record("sess1", "q1", "a1")
        btwlog.record("sess1", "q2", "a2")
        self.assertEqual(len(btwlog.entries("sess1")), 2)
        self.assertEqual(btwlog.latest("sess1")["question"], "q2")

    def test_empty_answer_not_recorded(self):
        self.assertIsNone(btwlog.record("sess1", "q1", "   "))
        self.assertEqual(btwlog.entries("sess1"), [])

    def test_persists_across_cache_drop(self):
        btwlog.record("sess1", "q1", "a1")
        btwlog._cache.clear()  # simulate a fresh process reading from disk
        self.assertEqual(btwlog.latest("sess1")["answer"], "a1")

    def test_clear_removes_disk_and_cache(self):
        btwlog.record("sess1", "q1", "a1")
        btwlog.clear("sess1")
        btwlog._cache.clear()
        self.assertEqual(btwlog.entries("sess1"), [])
        self.assertFalse((Path(self.tmp) / "sess1.jsonl").exists())

    def test_has_prefix_matches_stored_answer_prefix(self):
        btwlog.record("sess1", "q1", "L1\nL2\nL3\nL4")
        self.assertTrue(btwlog.has_prefix("sess1", "q1", "L1\nL2"))
        self.assertTrue(btwlog.has_prefix("sess1", "q1", "L1\nL2\nL3\nL4"))

    def test_has_prefix_false_on_mismatch(self):
        btwlog.record("sess1", "q1", "L1\nL2\nL3\nL4")
        self.assertFalse(btwlog.has_prefix("sess1", "q1", "X"))
        self.assertFalse(btwlog.has_prefix("sess1", "other", "L1"))
        self.assertFalse(btwlog.has_prefix("sess1", "q1", "   "))

    def test_dismiss_hides_latest_from_card(self):
        e = btwlog.record("sess1", "q1", "a1")
        self.assertTrue(btwlog.dismiss("sess1", e["id"]))
        self.assertIsNone(btwlog.latest("sess1"))

    def test_dismiss_persists_across_cache_drop(self):
        # The dismissal must survive a fleet restart, or the aside pops back.
        e = btwlog.record("sess1", "q1", "a1")
        btwlog.dismiss("sess1", e["id"])
        btwlog._cache.clear()
        self.assertIsNone(btwlog.latest("sess1"))

    def test_dismiss_unknown_id_is_false(self):
        btwlog.record("sess1", "q1", "a1")
        self.assertFalse(btwlog.dismiss("sess1", 999))
        self.assertIsNotNone(btwlog.latest("sess1"))

    def test_dismissed_stays_in_timeline(self):
        # Dismiss only affects the card's current-state view; the timeline is
        # history and keeps the aside.
        e = btwlog.record("sess1", "q1", "a1")
        btwlog.dismiss("sess1", e["id"])
        evs = btwlog.timeline_events("sess1")
        self.assertEqual([ev["kind"] for ev in evs], ["user_text", "assistant_text"])

    def test_new_aside_after_dismiss_shows_on_card(self):
        e = btwlog.record("sess1", "q1", "a1")
        btwlog.dismiss("sess1", e["id"])
        btwlog.record("sess1", "q2", "a2")
        self.assertEqual(btwlog.latest("sess1")["question"], "q2")

    def test_dismissed_aside_still_dedupes_rescrape(self):
        # The overlay may still be on-screen after a card dismiss; the 2s
        # re-scrape must not resurrect the aside as a fresh entry.
        e = btwlog.record("sess1", "q1", "a1")
        btwlog.dismiss("sess1", e["id"])
        self.assertIsNone(btwlog.record("sess1", "q1", "a1"))
        self.assertIsNone(btwlog.latest("sess1"))

    def test_ids_unique_across_restart(self):
        # Dismiss tombstones reference entries by id, so an id minted after a
        # restart must not collide with one already on disk.
        e1 = btwlog.record("sess1", "q1", "a1")
        btwlog._cache.clear()  # simulate a fresh process
        e2 = btwlog.record("sess1", "q2", "a2")
        self.assertNotEqual(e1["id"], e2["id"])

    def test_timeline_events_shape(self):
        btwlog.record("sess1", "q1", "a1")
        evs = btwlog.timeline_events("sess1")
        self.assertEqual([e["kind"] for e in evs], ["user_text", "assistant_text"])
        self.assertTrue(all(e["extra"]["source"] == "btw" for e in evs))
        self.assertTrue(evs[0]["text"].startswith("/btw "))
        self.assertEqual(evs[1]["text"], "a1")


class TimelineMergeTests(unittest.TestCase):
    def _patches(self, timeline_ret, btw_ret):
        w = types.SimpleNamespace(transcript_path="/x.jsonl", platform="claude",
                                  session_id="sess1", project_name="proj")
        return [
            mock.patch.object(appmod.sessions, "find_window", return_value=w),
            mock.patch.object(appmod.transcripts, "timeline", return_value=timeline_ret),
            mock.patch.object(appmod.btwlog, "timeline_events", return_value=btw_ret),
            mock.patch.object(appmod.transcripts, "extract_skills_used", return_value=[]),
            mock.patch.object(appmod.transcripts, "extract_memory_ops", return_value=[]),
            mock.patch.object(appmod.transcripts, "extract_plan_history", return_value=[]),
            mock.patch.object(appmod.actions, "get_pane_menu", return_value=None),
        ]

    def test_merges_btw_events_sorted_by_ts(self):
        real = [{"ts": "2026-07-01T21:20:00.000Z", "kind": "assistant_text",
                 "text": "later real turn", "tool": None, "role": "assistant", "extra": {}}]
        btw = [{"ts": "2026-07-01T21:10:00.000Z", "kind": "user_text",
                "text": "/btw q", "tool": None, "role": "user", "extra": {"source": "btw"}}]
        patches = self._patches(list(real), list(btw))
        for p in patches:
            p.start()
        try:
            out = appmod.api_timeline(123)
        finally:
            for p in patches:
                p.stop()
        # earlier /btw event sorts before the later real turn
        self.assertEqual(out["events"][0]["extra"].get("source"), "btw")
        self.assertEqual(out["events"][-1]["text"], "later real turn")

    def test_no_merge_when_no_asides(self):
        real = [{"ts": "2026-07-01T21:20:00.000Z", "kind": "assistant_text",
                 "text": "only turn", "tool": None, "role": "assistant", "extra": {}}]
        patches = self._patches(list(real), [])
        for p in patches:
            p.start()
        try:
            out = appmod.api_timeline(123)
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(len(out["events"]), 1)


class BtwCaptureGateTests(unittest.TestCase):
    """The gate that decides whether to fire the (slow, key-injecting) full-answer
    scroll-stitch: skip when the aside is already fully archived, and never run two
    at once for the same session."""

    def setUp(self):
        from core import btwcapture
        self.btwcapture = btwcapture
        self.tmp = tempfile.mkdtemp()
        self._orig = btwlog._DIR
        btwlog._DIR = Path(self.tmp)
        btwlog._cache.clear()
        btwcapture._inflight.clear()
        # Run the "background" work synchronously so the test is deterministic.
        self._thread_patch = mock.patch.object(
            btwcapture.threading, "Thread",
            side_effect=lambda target, args=(), daemon=None: types.SimpleNamespace(
                start=lambda: target(*args)))
        self._thread_patch.start()

    def tearDown(self):
        self._thread_patch.stop()
        btwlog._DIR = self._orig
        btwlog._cache.clear()
        self.btwcapture._inflight.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_captures_and_stores_new_aside(self):
        slice_ov = {"question": "q1", "answer": "L1\nL2"}
        full = {"question": "q1", "answer": "L1\nL2\nL3\nL4\nL5\nL6"}
        with mock.patch.object(self.btwcapture.actions, "get_btw_state",
                               return_value={"settled": slice_ov}), \
             mock.patch.object(self.btwcapture.actions, "capture_full_btw_answer",
                               return_value=full) as cf:
            got = self.btwcapture.maybe_capture(123, "sess1")
        cf.assert_called_once()
        self.assertIsNone(got)
        self.assertEqual(btwlog.latest("sess1")["answer"], "L1\nL2\nL3\nL4\nL5\nL6")

    def test_skips_already_archived_aside(self):
        btwlog.record("sess1", "q1", "L1\nL2\nL3\nL4\nL5\nL6")
        slice_ov = {"question": "q1", "answer": "L1\nL2"}  # top slice of the stored full answer
        with mock.patch.object(self.btwcapture.actions, "get_btw_state",
                               return_value={"settled": slice_ov}), \
             mock.patch.object(self.btwcapture.actions, "capture_full_btw_answer") as cf:
            self.btwcapture.maybe_capture(123, "sess1")
        cf.assert_not_called()  # already have it — no key injection

    def test_skips_when_no_overlay(self):
        with mock.patch.object(self.btwcapture.actions, "get_btw_state", return_value=None), \
             mock.patch.object(self.btwcapture.actions, "capture_full_btw_answer") as cf:
            self.btwcapture.maybe_capture(123, "sess1")
        cf.assert_not_called()

    def test_generating_aside_returns_pending_question(self):
        # Mid-generation: nothing to archive, but the question is surfaced so the
        # card can show a live "answering…" state instead of nothing.
        with mock.patch.object(self.btwcapture.actions, "get_btw_state",
                               return_value={"pending": "what is recall?"}), \
             mock.patch.object(self.btwcapture.actions, "capture_full_btw_answer") as cf:
            got = self.btwcapture.maybe_capture(123, "sess1")
        cf.assert_not_called()
        self.assertEqual(got, "what is recall?")


class CaptureSyncTests(unittest.TestCase):
    """Blocking archive for callers about to destroy the overlay (dismiss-Escape
    before a new prompt, the dashboard Esc button): the answer exists nowhere
    else, so it must be latched before the Escape, not on a later poll."""

    def setUp(self):
        from core import btwcapture
        self.btwcapture = btwcapture
        self.tmp = tempfile.mkdtemp()
        self._orig = btwlog._DIR
        btwlog._DIR = Path(self.tmp)
        btwlog._cache.clear()
        btwcapture._inflight.clear()

    def tearDown(self):
        btwlog._DIR = self._orig
        btwlog._cache.clear()
        self.btwcapture._inflight.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_archives_full_answer_before_returning(self):
        slice_ov = {"question": "q1", "answer": "L1\nL2"}
        full = {"question": "q1", "answer": "L1\nL2\nL3"}
        with mock.patch.object(self.btwcapture.actions, "get_btw_answer", return_value=slice_ov), \
             mock.patch.object(self.btwcapture.actions, "capture_full_btw_answer",
                               return_value=full):
            self.btwcapture.capture_sync(123, "sess1")
        self.assertEqual(btwlog.latest("sess1")["answer"], "L1\nL2\nL3")
        self.assertNotIn("sess1", self.btwcapture._inflight)

    def test_falls_back_to_top_slice_when_stitch_fails(self):
        # A truncated answer beats a vanished one.
        slice_ov = {"question": "q1", "answer": "L1\nL2"}
        with mock.patch.object(self.btwcapture.actions, "get_btw_answer", return_value=slice_ov), \
             mock.patch.object(self.btwcapture.actions, "capture_full_btw_answer",
                               side_effect=RuntimeError("pane gone")):
            self.btwcapture.capture_sync(123, "sess1")
        self.assertEqual(btwlog.latest("sess1")["answer"], "L1\nL2")

    def test_noop_when_already_archived(self):
        btwlog.record("sess1", "q1", "L1\nL2\nL3")
        slice_ov = {"question": "q1", "answer": "L1\nL2"}
        with mock.patch.object(self.btwcapture.actions, "get_btw_answer", return_value=slice_ov), \
             mock.patch.object(self.btwcapture.actions, "capture_full_btw_answer") as cf:
            self.btwcapture.capture_sync(123, "sess1")
        cf.assert_not_called()

    def test_noop_when_no_overlay(self):
        with mock.patch.object(self.btwcapture.actions, "get_btw_answer", return_value=None), \
             mock.patch.object(self.btwcapture.actions, "capture_full_btw_answer") as cf:
            self.btwcapture.capture_sync(123, "sess1")
        cf.assert_not_called()


if __name__ == "__main__":
    unittest.main()

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


if __name__ == "__main__":
    unittest.main()

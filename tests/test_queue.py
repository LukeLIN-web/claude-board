"""Tests for queued-prompt parsing (core.actions.parse_pane_queue) and the
reliable dashboard-sent tracker (core.promptqueue)."""
import time
import unittest
from unittest import mock

from core import actions, promptqueue


# Real captures collected from a live Claude pane.
CAP_THREE = """\
❯ Run this exact bash command and wait for it: sleep 40 && echo done
  this is queued message ONE

✶ Noodling… (3s · ↓ 25 tokens · thinking with high effort)


  ❯ second queued message here
  ❯ third one with more words in it
────────────────────────────────────────────────────────────────────────────────
❯ Press up to edit queued messages
────────────────────────────────────────────────────────────────────────────────
  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt
"""

CAP_TRANSITION = """\
❯ a FOURTH message appears

· Synthesizing…
  ⎿  Tip: Use /memory to view and manage Claude memory

────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
  ⏵⏵ bypass permissions on · 1 shell · esc to interrupt · ↓ to manage
"""

# Real capture with 3 queued: the first ("alpha", column-0 "❯") renders in the
# output stream; only the 2nd+ form the indented block above the box. The \xa0
# is the non-breaking space Claude puts in the placeholder line.
CAP_FIRST_AT_COL0 = """\
  └ \xa0Error: Blocked: sleep 50 followed by: echo done.
❯ alpha queued prompt
● Running it in the background instead.
✶ Bunning… (7s · ↓ 341 tokens)
  ❯ beta queued prompt
  ❯ gamma queued prompt
────────────────────────────────────────────────────────────────────────────────
❯\xa0Press up to edit queued messages
────────────────────────────────────────────────────────────────────────────────
  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt
"""

CAP_NONE = """\
✻ Sautéing… (46s · ↑ 1.9k tokens)

────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt
"""


class ParsePaneQueueTests(unittest.TestCase):
    def test_multi_item_block_above_input_box(self):
        self.assertEqual(
            actions.parse_pane_queue(CAP_THREE),
            ["second queued message here", "third one with more words in it"],
        )

    def test_first_queued_at_col0_missed_rest_captured(self):
        # The first message ("alpha", column-0) is not recoverable; the indented
        # block (beta, gamma) above the input box is.
        self.assertEqual(
            actions.parse_pane_queue(CAP_FIRST_AT_COL0),
            ["beta queued prompt", "gamma queued prompt"],
        )

    def test_transition_single_item_is_missed_best_effort(self):
        # The single/transition rendering sits away from the box; documented gap.
        self.assertEqual(actions.parse_pane_queue(CAP_TRANSITION), [])

    def test_no_queue_returns_empty(self):
        self.assertEqual(actions.parse_pane_queue(CAP_NONE), [])

    def test_empty_text(self):
        self.assertEqual(actions.parse_pane_queue(""), [])


class PromptQueueTests(unittest.TestCase):
    def setUp(self):
        promptqueue._sent.clear()

    def test_record_then_pending_no_transcript(self):
        promptqueue.record_sent(1, "do the thing")
        out = promptqueue.pending(1, None, "busy")
        self.assertEqual([o["text"] for o in out], ["do the thing"])

    def test_blank_prompt_not_recorded(self):
        promptqueue.record_sent(1, "   \n  ")
        self.assertEqual(promptqueue.pending(1, None, "busy"), [])

    def test_idle_clears_queue(self):
        promptqueue.record_sent(1, "x")
        self.assertEqual(promptqueue.pending(1, None, "idle"), [])
        # Tracker is wiped, so a later busy tick stays empty.
        self.assertEqual(promptqueue.pending(1, None, "busy"), [])

    def test_consumed_when_seen_in_transcript(self):
        promptqueue.record_sent(1, "run tests")
        future = time.time() + 10
        with mock.patch.object(promptqueue.transcripts, "recent_user_texts",
                               return_value=[(future, "run tests")]):
            out = promptqueue.pending(1, "t.jsonl", "busy")
        self.assertEqual(out, [])

    def test_old_identical_message_does_not_consume(self):
        promptqueue.record_sent(1, "run tests")
        with mock.patch.object(promptqueue.transcripts, "recent_user_texts",
                               return_value=[(0.0, "run tests")]):  # ts before send
            out = promptqueue.pending(1, "t.jsonl", "busy")
        self.assertEqual([o["text"] for o in out], ["run tests"])

    def test_duplicate_sends_clear_one_per_transcript_hit(self):
        promptqueue.record_sent(1, "ping")
        promptqueue.record_sent(1, "ping")
        future = time.time() + 10
        with mock.patch.object(promptqueue.transcripts, "recent_user_texts",
                               return_value=[(future, "ping")]):  # only one picked up
            out = promptqueue.pending(1, "t.jsonl", "busy")
        self.assertEqual([o["text"] for o in out], ["ping"])  # one still queued

    def test_slash_command_reconciles_against_bare_label(self):
        # The dashboard sends "/btw"; the transcript logs it as the bare label
        # "btw" (transcripts._clean_command_text drops the envelope + slash). The
        # two must still match or the command sticks in the queue forever.
        promptqueue.record_sent(1, "/btw")
        future = time.time() + 10
        with mock.patch.object(promptqueue.transcripts, "recent_user_texts",
                               return_value=[(future, "btw")]):
            out = promptqueue.pending(1, "t.jsonl", "busy")
        self.assertEqual(out, [])

    def test_slash_command_display_text_keeps_slash(self):
        # Match is slash-insensitive, but the card label keeps the "/" the user typed.
        promptqueue.record_sent(1, "/btw")
        out = promptqueue.pending(1, None, "busy")
        self.assertEqual([o["text"] for o in out], ["/btw"])


if __name__ == "__main__":
    unittest.main()

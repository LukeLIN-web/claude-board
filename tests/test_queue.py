"""Tests for queued-prompt parsing (core.actions.parse_pane_queue) and the
reliable dashboard-sent tracker (core.promptqueue)."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from core import actions, promptqueue, transcripts


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
        with mock.patch.object(promptqueue.transcripts, "consumed_prompt_texts",
                               return_value=[(future, "run tests")]):
            out = promptqueue.pending(1, "t.jsonl", "busy")
        self.assertEqual(out, [])

    def test_old_identical_message_does_not_consume(self):
        promptqueue.record_sent(1, "run tests")
        with mock.patch.object(promptqueue.transcripts, "consumed_prompt_texts",
                               return_value=[(0.0, "run tests")]):  # ts before send
            out = promptqueue.pending(1, "t.jsonl", "busy")
        self.assertEqual([o["text"] for o in out], ["run tests"])

    def test_duplicate_sends_clear_one_per_transcript_hit(self):
        promptqueue.record_sent(1, "ping")
        promptqueue.record_sent(1, "ping")
        future = time.time() + 10
        with mock.patch.object(promptqueue.transcripts, "consumed_prompt_texts",
                               return_value=[(future, "ping")]):  # only one picked up
            out = promptqueue.pending(1, "t.jsonl", "busy")
        self.assertEqual([o["text"] for o in out], ["ping"])  # one still queued

    def test_slash_command_reconciles_against_bare_label(self):
        # The dashboard sends "/btw"; the transcript logs it as the bare label
        # "btw" (transcripts._clean_command_text drops the envelope + slash). The
        # two must still match or the command sticks in the queue forever.
        promptqueue.record_sent(1, "/btw")
        future = time.time() + 10
        with mock.patch.object(promptqueue.transcripts, "consumed_prompt_texts",
                               return_value=[(future, "btw")]):
            out = promptqueue.pending(1, "t.jsonl", "busy")
        self.assertEqual(out, [])

    def test_slash_command_display_text_keeps_slash(self):
        # Match is slash-insensitive, but the card label keeps the "/" the user typed.
        promptqueue.record_sent(1, "/btw")
        out = promptqueue.pending(1, None, "busy")
        self.assertEqual([o["text"] for o in out], ["/btw"])

    def test_fifo_sweep_drops_older_item_once_a_newer_one_is_consumed(self):
        # Claude drains its queue in order, so if a later send was picked up, an
        # earlier one can never still be pending — it was swallowed (or its row
        # went to a transcript we can't see). Without this, a swallowed prompt
        # sticks on the card until the session idles.
        promptqueue.record_sent(1, "swallowed")
        promptqueue.record_sent(1, "landed")
        future = time.time() + 10
        with mock.patch.object(promptqueue.transcripts, "consumed_prompt_texts",
                               return_value=[(future, "landed")]):
            out = promptqueue.pending(1, "t.jsonl", "busy")
        self.assertEqual(out, [])


def _iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


class ConsumedPromptTextsTests(unittest.TestCase):
    """transcripts.consumed_prompt_texts: every signal that Claude took a prompt."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "t.jsonl"
        self.t0 = time.time()

    def _write(self, rows: list[dict]) -> None:
        self.path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def _user_row(self, ts: float, text: str) -> dict:
        return {"type": "user", "timestamp": _iso(ts),
                "message": {"role": "user", "content": text}}

    def _noise_row(self, ts: float) -> dict:
        return {"type": "assistant", "timestamp": _iso(ts),
                "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}}

    def test_queue_operation_remove_counts_as_consumed(self):
        # A prompt sent while Claude is busy never gets a type:"user" row — the
        # only trace is queue-operation enqueue then remove. `remove` is Claude
        # taking it off its queue, i.e. exactly the signal we reconcile against.
        self._write([
            {"type": "queue-operation", "operation": "enqueue",
             "timestamp": _iso(self.t0 + 1), "content": "look at the loss first"},
            {"type": "queue-operation", "operation": "remove",
             "timestamp": _iso(self.t0 + 5), "content": "look at the loss first"},
        ])
        got = transcripts.consumed_prompt_texts(self.path, since=self.t0)
        self.assertEqual([t for _, t in got], ["look at the loss first"])

    def test_enqueue_alone_is_not_consumed(self):
        # Still sitting in Claude's queue: the card SHOULD keep showing it.
        self._write([
            {"type": "queue-operation", "operation": "enqueue",
             "timestamp": _iso(self.t0 + 1), "content": "still waiting"},
        ])
        self.assertEqual(transcripts.consumed_prompt_texts(self.path, since=self.t0), [])

    def test_user_row_far_beyond_the_old_100_line_window(self):
        # The old reconcile window was the last 100 raw rows; a busy session
        # buries the user row under tool noise, so it never reconciled.
        rows = [self._user_row(self.t0 + 1, "update the docs")]
        rows += [self._noise_row(self.t0 + 2 + i) for i in range(300)]
        self._write(rows)
        got = transcripts.consumed_prompt_texts(self.path, since=self.t0)
        self.assertEqual([t for _, t in got], ["update the docs"])

    def test_rows_before_since_are_ignored(self):
        self._write([self._user_row(self.t0 - 100, "ancient prompt")])
        self.assertEqual(transcripts.consumed_prompt_texts(self.path, since=self.t0), [])

    def test_missing_file(self):
        self.assertEqual(transcripts.consumed_prompt_texts("/nope.jsonl", since=0.0), [])


class QueueReconcileIntegrationTests(unittest.TestCase):
    """End-to-end: record a send, then reconcile against a real transcript file."""

    def setUp(self):
        promptqueue._sent.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "t.jsonl"

    def test_busy_send_clears_once_claude_dequeues_it(self):
        t0 = time.time()
        promptqueue.record_sent(1, "run the eval", ts=t0)
        # Claude is busy: the prompt is enqueued, then dequeued 5s later. Plenty
        # of tool noise follows, pushing the rows out of any fixed line window.
        rows = [
            {"type": "queue-operation", "operation": "enqueue",
             "timestamp": _iso(t0 + 1), "content": "run the eval"},
            {"type": "queue-operation", "operation": "remove",
             "timestamp": _iso(t0 + 5), "content": "run the eval"},
        ]
        rows += [{"type": "assistant", "timestamp": _iso(t0 + 6 + i),
                  "message": {"role": "assistant", "content": [
                      {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}}
                 for i in range(200)]
        self.path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.assertEqual(promptqueue.pending(1, str(self.path), "busy"), [])

    def test_still_enqueued_prompt_stays_on_the_card(self):
        t0 = time.time()
        promptqueue.record_sent(1, "run the eval", ts=t0)
        self.path.write_text(json.dumps({
            "type": "queue-operation", "operation": "enqueue",
            "timestamp": _iso(t0 + 1), "content": "run the eval"}) + "\n")
        out = promptqueue.pending(1, str(self.path), "busy")
        self.assertEqual([o["text"] for o in out], ["run the eval"])


if __name__ == "__main__":
    unittest.main()

"""Which model a session is running on, read from the transcript.

The transcript is the only honest source: it records the model that actually
answered. A switch driven from the board can silently fail to land (see
actions.switch_model), and a switch typed straight into the TUI never touches the
board at all — so anything derived from what the board *did* would lie in both
directions. The cost is lag: nothing shows until the session next replies.
"""
import json
import unittest

from core import transcripts
from tests.helpers import assistant_row, scratch_dir, user_row, write_jsonl


class CurrentModelTests(unittest.TestCase):
    def test_is_the_last_assistant_row_not_the_first(self):
        p = write_jsonl([
            assistant_row("hi", "2026-07-14T21:00:00Z", model="claude-opus-4-8"),
            user_row("go", "2026-07-14T21:01:00Z", blocks=True),
            assistant_row("hi", "2026-07-14T21:02:00Z", model="claude-fable-5"),
        ])
        self.assertEqual(transcripts.current_model(p), "claude-fable-5")

    def test_empty_when_no_assistant_turn_yet(self):
        p = write_jsonl([user_row("go", "2026-07-14T21:00:00Z", blocks=True)])
        self.assertEqual(transcripts.current_model(p), "")

    def test_empty_when_transcript_missing(self):
        self.assertEqual(transcripts.current_model("/nope/nothing.jsonl"), "")

    def test_synthetic_rows_do_not_count(self):
        # Claude stamps model "<synthetic>" on placeholder assistant rows it writes
        # itself ("No response requested.", API errors). No model ran those.
        p = write_jsonl([
            assistant_row("hi", "2026-07-14T21:00:00Z", model="claude-opus-4-8"),
            assistant_row("No response requested.", "2026-07-14T21:02:00Z", model="<synthetic>"),
        ])
        self.assertEqual(transcripts.current_model(p), "claude-opus-4-8")


class PrettyModelTests(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(transcripts.pretty_model("claude-opus-4-8"), "Opus 4.8")
        self.assertEqual(transcripts.pretty_model("claude-fable-5"), "Fable 5")
        self.assertEqual(transcripts.pretty_model("claude-haiku-4-5-20251001"), "Haiku 4.5")

    def test_family_after_version_in_older_ids(self):
        self.assertEqual(transcripts.pretty_model("claude-3-5-sonnet-20241022"), "Sonnet 3.5")

    def test_unrecognized_id_is_passed_through(self):
        self.assertEqual(transcripts.pretty_model("gpt-5"), "gpt-5")
        self.assertEqual(transcripts.pretty_model(""), "")


class TimelineModelEventTests(unittest.TestCase):
    def _kinds(self, events):
        return [(e["kind"], e["text"]) for e in events if e["kind"] == "model"]

    def test_switch_shows_up_as_an_event(self):
        p = write_jsonl([
            assistant_row("hi", "2026-07-14T21:00:00Z", model="claude-opus-4-8"),
            user_row("go", "2026-07-14T21:01:00Z", blocks=True),
            assistant_row("now on fable", "2026-07-14T21:02:00Z", model="claude-fable-5"),
        ])
        evs = transcripts.timeline(p)
        self.assertEqual(self._kinds(evs), [("model", "Model → Fable 5")])
        ev = next(e for e in evs if e["kind"] == "model")
        self.assertEqual(ev["role"], "system")
        self.assertEqual(ev["extra"], {"model": "claude-fable-5"})
        # Placed at the turn that first ran on the new model, ahead of its text.
        self.assertLess(evs.index(ev),
                        next(i for i, e in enumerate(evs) if e["text"] == "now on fable"))

    def test_no_event_when_the_model_never_changes(self):
        p = write_jsonl([
            assistant_row("hi", "2026-07-14T21:00:00Z", model="claude-opus-4-8"),
            user_row("go", "2026-07-14T21:01:00Z", blocks=True),
            assistant_row("hi", "2026-07-14T21:02:00Z", model="claude-opus-4-8"),
        ])
        self.assertEqual(self._kinds(transcripts.timeline(p)), [])

    def test_synthetic_rows_are_not_switches(self):
        # A "<synthetic>" row between two real turns would otherwise read as two
        # switches (away and back), neither of which happened.
        p = write_jsonl([
            assistant_row("hi", "2026-07-14T21:00:00Z", model="claude-opus-4-8"),
            assistant_row("No response requested.", "2026-07-14T21:01:00Z", model="<synthetic>"),
            assistant_row("hi", "2026-07-14T21:02:00Z", model="claude-opus-4-8"),
        ])
        self.assertEqual(self._kinds(transcripts.timeline(p)), [])

    def test_first_model_in_the_window_is_not_a_change(self):
        # timeline() only reads a tail, so the earliest row it sees has no
        # predecessor to compare against — it must not manufacture a switch.
        p = write_jsonl([assistant_row("hi", "2026-07-14T21:00:00Z", model="claude-opus-4-8")])
        self.assertEqual(self._kinds(transcripts.timeline(p)), [])


class MemoTests(unittest.TestCase):
    def test_a_transcript_that_grows_is_read_again(self):
        # Memoized on (mtime, size): an append always changes the size, so the
        # card can't stay on the model it read before the session switched.
        p = write_jsonl([assistant_row("hi", "2026-07-14T21:00:00Z", model="claude-opus-4-8")])
        self.assertEqual(transcripts.current_model(p), "claude-opus-4-8")
        with p.open("a") as f:
            f.write(json.dumps(assistant_row("hi", "2026-07-14T21:02:00Z", model="claude-fable-5")) + "\n")
        self.assertEqual(transcripts.current_model(p), "claude-fable-5")


class TailRawLinesTests(unittest.TestCase):
    """tail_raw_lines reads backward from the end, so where its blocks start
    and stop must never show in what it returns."""

    def _file(self, text: str):
        p = scratch_dir() / "t.jsonl"
        p.write_text(text)
        return p

    def test_a_line_cut_by_a_block_boundary_is_not_returned(self):
        # 100-byte lines: a 64 KB block starts partway through one of them.
        lines = [f"{i:099d}" for i in range(3000)]
        p = self._file("".join(ln + "\n" for ln in lines))
        self.assertEqual(transcripts.tail_raw_lines(p, 5), lines[-5:])
        # More than one block back.
        self.assertEqual(transcripts.tail_raw_lines(p, 1500), lines[-1500:])

    def test_a_last_line_with_no_newline_is_kept(self):
        p = self._file('{"a": 1}\n{"b": 2}\n{"c": 3')
        self.assertEqual(transcripts.tail_raw_lines(p, 2), ['{"b": 2}', '{"c": 3'])

    def test_a_file_shorter_than_asked_for_comes_back_whole(self):
        p = self._file("one\ntwo\n")
        self.assertEqual(transcripts.tail_raw_lines(p, 10), ["one", "two"])

    def test_missing_file(self):
        self.assertEqual(transcripts.tail_raw_lines("/nope/nothing.jsonl", 5), [])

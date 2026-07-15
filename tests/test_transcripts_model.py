"""Which model a session is running on, read from the transcript.

The transcript is the only honest source: it records the model that actually
answered. A switch driven from the board can silently fail to land (see
actions.switch_model), and a switch typed straight into the TUI never touches the
board at all — so anything derived from what the board *did* would lie in both
directions. The cost is lag: nothing shows until the session next replies.
"""
import json
import tempfile
import unittest
from pathlib import Path

from core import transcripts


def _write(rows) -> Path:
    d = Path(tempfile.mkdtemp())
    p = d / "t.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return p


def _assistant(ts, model, text="hi"):
    return {"type": "assistant", "timestamp": ts,
            "message": {"model": model, "content": [{"type": "text", "text": text}]}}


def _user(ts, text="go"):
    return {"type": "user", "timestamp": ts,
            "message": {"content": [{"type": "text", "text": text}]}}


class CurrentModelTests(unittest.TestCase):
    def test_is_the_last_assistant_row_not_the_first(self):
        p = _write([
            _assistant("2026-07-14T21:00:00Z", "claude-opus-4-8"),
            _user("2026-07-14T21:01:00Z"),
            _assistant("2026-07-14T21:02:00Z", "claude-fable-5"),
        ])
        self.assertEqual(transcripts.current_model(p), "claude-fable-5")

    def test_empty_when_no_assistant_turn_yet(self):
        p = _write([_user("2026-07-14T21:00:00Z")])
        self.assertEqual(transcripts.current_model(p), "")

    def test_empty_when_transcript_missing(self):
        self.assertEqual(transcripts.current_model("/nope/nothing.jsonl"), "")

    def test_synthetic_rows_do_not_count(self):
        # Claude stamps model "<synthetic>" on placeholder assistant rows it writes
        # itself ("No response requested.", API errors). No model ran those.
        p = _write([
            _assistant("2026-07-14T21:00:00Z", "claude-opus-4-8"),
            _assistant("2026-07-14T21:02:00Z", "<synthetic>", "No response requested."),
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
        p = _write([
            _assistant("2026-07-14T21:00:00Z", "claude-opus-4-8"),
            _user("2026-07-14T21:01:00Z"),
            _assistant("2026-07-14T21:02:00Z", "claude-fable-5", "now on fable"),
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
        p = _write([
            _assistant("2026-07-14T21:00:00Z", "claude-opus-4-8"),
            _user("2026-07-14T21:01:00Z"),
            _assistant("2026-07-14T21:02:00Z", "claude-opus-4-8"),
        ])
        self.assertEqual(self._kinds(transcripts.timeline(p)), [])

    def test_synthetic_rows_are_not_switches(self):
        # A "<synthetic>" row between two real turns would otherwise read as two
        # switches (away and back), neither of which happened.
        p = _write([
            _assistant("2026-07-14T21:00:00Z", "claude-opus-4-8"),
            _assistant("2026-07-14T21:01:00Z", "<synthetic>", "No response requested."),
            _assistant("2026-07-14T21:02:00Z", "claude-opus-4-8"),
        ])
        self.assertEqual(self._kinds(transcripts.timeline(p)), [])

    def test_first_model_in_the_window_is_not_a_change(self):
        # timeline() only reads a tail, so the earliest row it sees has no
        # predecessor to compare against — it must not manufacture a switch.
        p = _write([_assistant("2026-07-14T21:00:00Z", "claude-opus-4-8")])
        self.assertEqual(self._kinds(transcripts.timeline(p)), [])


if __name__ == "__main__":
    unittest.main()

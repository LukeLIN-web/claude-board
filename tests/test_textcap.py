"""Timeline text is capped, but never silently.

The board re-fetches an open timeline every couple of seconds, so raw transcript
text can't go over the wire whole. What matters is that a cut is *visible*: a
message that ends mid-sentence with no marker reads as a finished thought, which
is exactly the bug these caps used to have.
"""
import json
import tempfile
import unittest
from pathlib import Path

from core import transcripts
from core.textcap import META_CHARS, MESSAGE_CHARS, TOOL_RESULT_CHARS, cap_text


def _write(rows) -> Path:
    d = Path(tempfile.mkdtemp())
    p = d / "t.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return p


class CapTextTests(unittest.TestCase):
    def test_short_text_is_untouched(self):
        self.assertEqual(cap_text("hello", 10), "hello")
        self.assertEqual(cap_text("exactly10!", 10), "exactly10!")

    def test_none_is_empty(self):
        self.assertEqual(cap_text(None, 10), "")

    def test_cut_text_says_how_much_it_dropped(self):
        out = cap_text("x" * 30, 10)
        self.assertTrue(out.startswith("x" * 10))
        self.assertIn("20", out)  # the 20 dropped chars are named


class TimelineCapTests(unittest.TestCase):
    def test_long_message_survives_far_past_the_old_4000_cap(self):
        text = "字" * 8000
        p = _write([{"type": "assistant", "timestamp": "2026-08-05T10:00:00Z",
                     "message": {"model": "claude-opus-5",
                                 "content": [{"type": "text", "text": text}]}}])
        ev = transcripts.timeline(p)[0]
        self.assertEqual(ev["kind"], "assistant_text")
        self.assertEqual(ev["text"], text)

    def test_tool_result_is_capped_with_a_marker(self):
        out = "y" * (TOOL_RESULT_CHARS + 500)
        p = _write([{"type": "user", "timestamp": "2026-08-05T10:00:00Z",
                     "message": {"content": [{"type": "tool_result", "content": out}]}}])
        ev = transcripts.timeline(p)[0]
        self.assertEqual(ev["kind"], "tool_result")
        self.assertTrue(ev["text"].startswith("y" * TOOL_RESULT_CHARS))
        self.assertIn("500", ev["text"])

    def test_answer_echo_is_kept_whole(self):
        # Not tool output — the user's own selection, worth reading in full.
        out = "Your questions have been answered" + "z" * (TOOL_RESULT_CHARS + 500)
        p = _write([{"type": "user", "timestamp": "2026-08-05T10:00:00Z",
                     "message": {"content": [{"type": "tool_result", "content": out}]}}])
        ev = transcripts.timeline(p)[0]
        self.assertLessEqual(len(out), MESSAGE_CHARS)
        self.assertEqual(ev["text"], out)

    def test_injected_skill_body_is_a_trace_not_a_wall(self):
        # Claude logs a skill's whole SKILL.md as an `isMeta` user row — 150k
        # chars for one /update-config. Nobody typed it, so it gets the short cap.
        body = "# Update Config Skill\n" + "说明" * 60000
        p = _write([{"type": "user", "isMeta": True, "timestamp": "2026-08-05T10:00:00Z",
                     "message": {"content": [{"type": "text", "text": body}]}}])
        ev = transcripts.timeline(p)[0]
        self.assertEqual(ev["kind"], "user_text")
        self.assertTrue(ev["text"].startswith("# Update Config Skill"))
        self.assertLess(len(ev["text"]), META_CHARS + 60)
        self.assertIn(str(len(body) - META_CHARS), ev["text"])  # names the rest
        self.assertTrue(ev["extra"]["meta"])  # the client labels it as injected

    def test_a_typed_prompt_keeps_the_generous_cap(self):
        text = "字" * 8000
        p = _write([{"type": "user", "timestamp": "2026-08-05T10:00:00Z",
                     "message": {"content": [{"type": "text", "text": text}]}}])
        ev = transcripts.timeline(p)[0]
        self.assertEqual(ev["text"], text)
        self.assertNotIn("meta", ev["extra"])


if __name__ == "__main__":
    unittest.main()

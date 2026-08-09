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
from core.textcap import (
    EDIT_DIFF_CHARS,
    META_CHARS,
    MESSAGE_CHARS,
    TOOL_RESULT_CHARS,
    cap_text,
    edit_diff,
)


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


class EditDiffTests(unittest.TestCase):
    """An Edit's two strings are near-identical — the row must show the change."""

    def _tagged(self, diff):
        """({deleted lines}, {added lines}) — what a reader sees under - and +."""
        dels = {ln[2:] for ln in diff.split("\n") if ln.startswith("- ")}
        adds = {ln[2:] for ln in diff.split("\n") if ln.startswith("+ ")}
        return dels, adds

    def test_a_deletion_reads_as_a_deletion(self):
        # The bug this replaced: two blocks of "…（N 行相同）" and nothing else,
        # leaving the reader to work out which lines had gone.
        head = "".join(f"    line {i}\n" for i in range(8))
        diff = edit_diff(head + "    gone = 1\n    also_gone = 2\n", head)
        dels, adds = self._tagged(diff)
        self.assertEqual(dels, {"    gone = 1", "    also_gone = 2"})
        self.assertEqual(adds, set())
        self.assertIn("…（8 行相同）", diff)  # named, not silently dropped
        self.assertNotIn("line 3", diff)

    def test_shared_lines_around_a_change_are_named_not_shown(self):
        head = "".join(f"    line {i}\n" for i in range(8))
        tail = "".join(f"    tail {i}\n" for i in range(8))
        diff = edit_diff(head + "    a = 1\n" + tail, head + "    a = 2\n" + tail)
        dels, adds = self._tagged(diff)
        self.assertEqual((dels, adds), ({"    a = 1"}, {"    a = 2"}))
        self.assertEqual(diff.count("…（8 行相同）"), 2)  # head and tail both

    def test_a_short_unchanged_run_is_shown_as_context(self):
        # Cheaper to print than to describe, and it locates the change.
        diff = edit_diff("keep me\ndrop me\n", "keep me\n")
        self.assertIn("  keep me", diff)
        self.assertEqual(self._tagged(diff), ({"drop me"}, set()))

    def test_a_rewritten_line_drops_the_chars_it_kept(self):
        pad = "#" * 100
        diff = edit_diff(f"value = compute(alpha, beta) {pad}",
                         f"value = compute(alpha, BETA) {pad}")
        dels, adds = self._tagged(diff)
        self.assertTrue(any("beta" in d and "字相同" in d for d in dels))
        self.assertTrue(any("BETA" in a for a in adds))
        self.assertNotIn(pad, diff)  # the 100 shared chars don't ship

    def test_a_short_line_rewrite_is_left_whole(self):
        # A few shared chars are context worth reading, not noise.
        self.assertEqual(edit_diff("hello world", "hello there"),
                         "- hello world\n+ hello there")

    def test_a_pure_insert_is_all_plus_lines(self):
        self.assertEqual(edit_diff("", "brand new"), "+ brand new")
        self.assertEqual(edit_diff(None, None), "")

    def test_a_whitespace_only_change_still_says_something(self):
        # splitlines() can't see it, so the row would otherwise render blank.
        self.assertIn("空白", edit_diff("a\n", "a"))

    def test_a_huge_edit_skips_the_matcher_but_still_diffs(self):
        old = "".join(f"old {i}\n" for i in range(400))
        new = "".join(f"new {i}\n" for i in range(400))
        diff = edit_diff(old, new, limit=10_000)
        self.assertIn("- old 0", diff)
        self.assertIn("+ new 0", diff)


class EditRowTests(unittest.TestCase):
    def _edit_row(self, inp):
        p = _write([{"type": "assistant", "timestamp": "2026-08-05T10:00:00Z",
                     "message": {"content": [{"type": "tool_use", "name": "Edit",
                                              "id": "t1", "input": inp}]}}])
        return transcripts.timeline(p)[0]

    def test_the_change_survives_the_arg_cap(self):
        # Pre-fix, both strings were cut at TOOL_ARG_CHARS of shared prefix and
        # the row showed everything except the edit.
        head = "".join(f"line {i} padding padding padding\n" for i in range(40))
        ev = self._edit_row({
            "file_path": "/repo/a.py",
            "old_string": head + "offset = rng.uniform(lo, hi)\n",
            "new_string": head + "offset = cum\n",
        })
        self.assertEqual(ev["kind"], "tool_use")
        self.assertEqual(ev["tool"], "Edit")
        self.assertEqual(ev["extra"]["file_path"], "/repo/a.py")
        diff = ev["extra"]["diff"]
        self.assertIn("- offset = rng.uniform(lo, hi)", diff)
        self.assertIn("+ offset = cum", diff)
        self.assertNotIn("line 20 padding", diff)
        self.assertLessEqual(len(diff), EDIT_DIFF_CHARS + 40)

    def test_a_still_huge_change_keeps_the_cap_marker(self):
        ev = self._edit_row({"file_path": "/repo/a.py", "old_string": "",
                             "new_string": "新" * (EDIT_DIFF_CHARS + 300)})
        self.assertIn("302", ev["extra"]["diff"])  # the 300 chars + the "+ " gutter

    def test_replace_all_is_kept_but_other_args_are_not_needed(self):
        ev = self._edit_row({"file_path": "/repo/a.py", "old_string": "a",
                             "new_string": "b", "replace_all": True})
        self.assertTrue(ev["extra"]["replace_all"])
        self.assertEqual(set(ev["extra"]), {"file_path", "diff", "replace_all"})

    def test_a_memory_edit_still_reads_as_a_memory_row(self):
        ev = self._edit_row({"file_path": "/home/u/.claude/memory/foo.md",
                             "old_string": "a", "new_string": "b"})
        self.assertEqual(ev["kind"], "memory_write")


if __name__ == "__main__":
    unittest.main()

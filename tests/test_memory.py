"""Memory files: which lines are frontmatter, and which of its keys are fields.

Claude Code has written a memory's frontmatter in two shapes — `type: user` at
the top level, and `metadata:` with `type` indented under it — and the board
groups memories by that type, so a misread files a memory under the wrong
heading. The body is what the page shows when a memory is opened; a misread
there cuts it off without a word.
"""
import unittest
from unittest import mock

import app as appmod
from core import memory
from tests.helpers import scratch_dir


def _fields(text):
    return memory.split_frontmatter(text)[0]


class FrontmatterFieldTests(unittest.TestCase):
    def test_top_level_type_wins_over_metadata_type(self):
        # Whichever comes first in the file.
        for text in ("---\nname: a\ntype: user\nmetadata:\n  type: feedback\n---\nbody\n",
                     "---\nname: a\nmetadata:\n  type: feedback\ntype: user\n---\nbody\n"):
            with self.subTest(text=text):
                self.assertEqual(_fields(text)["type"], "user")

    def test_metadata_type_stands_in_for_a_missing_top_level_type(self):
        text = ("---\nname: a\ndescription: d\nmetadata:\n  type: feedback\n"
                "  node_type: memory\n---\nbody\n")
        fm = _fields(text)
        self.assertEqual(fm["type"], "feedback")
        self.assertEqual(fm["name"], "a")
        self.assertEqual(fm["description"], "d")

    def test_nested_keys_never_replace_top_level_ones(self):
        text = ("---\nname: top-name\ndescription: top description\nmetadata:\n"
                "  name: nested-name\n  description: nested description\n---\n")
        fm = _fields(text)
        self.assertEqual(fm["name"], "top-name")
        self.assertEqual(fm["description"], "top description")

    def test_keys_under_another_parent_are_not_fields(self):
        text = "---\nname: a\nhooks:\n  type: command\n---\nbody\n"
        self.assertNotIn("type", _fields(text))

    def test_only_direct_children_of_metadata_stand_in(self):
        text = "---\nname: a\nmetadata:\n  extra:\n    type: deep\n  type: project\n---\n"
        self.assertEqual(_fields(text)["type"], "project")

    def test_crlf_line_endings(self):
        text = "---\r\nname: a\r\nmetadata:\r\n  type: user\r\n---\r\n\r\nbody line\r\n"
        fm, body = memory.split_frontmatter(text)
        self.assertEqual(fm, {"name": "a", "type": "user"})
        self.assertEqual(body, "body line")


class FrontmatterBodyTests(unittest.TestCase):
    def test_file_without_frontmatter_is_all_body_past_a_horizontal_rule(self):
        text = "# Notes\n\nfirst part\n\n---\n\nsecond part\n"
        fm, body = memory.split_frontmatter(text)
        self.assertEqual(fm, {})
        self.assertEqual(body, text)

    def test_horizontal_rule_in_a_body_after_frontmatter_is_kept(self):
        text = "---\nname: a\ntype: user\n---\n\nfirst part\n\n---\n\nsecond part\n"
        fm, body = memory.split_frontmatter(text)
        self.assertEqual(fm["type"], "user")
        self.assertEqual(body, "first part\n\n---\n\nsecond part")

    def test_a_rule_that_is_not_on_the_first_line_opens_no_frontmatter(self):
        text = "intro\n---\nname: a\n---\nrest\n"
        self.assertEqual(memory.split_frontmatter(text), ({}, text))

    def test_unclosed_frontmatter_is_all_body(self):
        text = "---\nname: a\nno closing fence\n"
        self.assertEqual(memory.split_frontmatter(text), ({}, text))


class MemoryRouteTests(unittest.TestCase):
    """The list and the detail route both read a file through split_frontmatter."""

    def setUp(self):
        projects = scratch_dir()
        self.mem = projects / "proj" / "memory"
        self.mem.mkdir(parents=True)
        patcher = mock.patch.object(memory, "PROJECTS_DIR", projects)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_list_groups_by_top_level_type_and_previews_the_whole_body(self):
        (self.mem / "both.md").write_text(
            "---\nname: both\ntype: user\nmetadata:\n  type: feedback\n---\nbody\n")
        (self.mem / "nested.md").write_text(
            "---\nname: nested\nmetadata:\n  type: project\n---\nbody\n")
        (self.mem / "plain.md").write_text("first part\n\n---\n\nsecond part\n")
        groups = memory.list_memories("proj")["groups"]
        self.assertEqual({t: [m["name"] for m in ms] for t, ms in groups.items()},
                         {"user": ["both"], "project": ["nested"], "unknown": ["plain"]})
        self.assertIn("second part", groups["unknown"][0]["content_preview"])

    def test_detail_shows_the_whole_body_of_a_file_without_frontmatter(self):
        (self.mem / "plain.md").write_text("first part\n\n---\n\nsecond part\n")
        d = appmod.api_memory_detail("plain")
        self.assertEqual(d["type"], "unknown")
        self.assertEqual(d["content"], "first part\n\n---\n\nsecond part\n")


if __name__ == "__main__":
    unittest.main()

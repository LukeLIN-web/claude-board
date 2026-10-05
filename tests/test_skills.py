"""The Skills panel's one line per skill: its description.

A SKILL.md opens with YAML frontmatter whose `description` is the text Claude
Code itself shows for the skill and matches requests against. Real files write
it plain, in double quotes (with `\\"` escapes), folded (`>-`) over several
lines, or quoted on the lines below the key; the panel should show the same
sentence in every case.
"""
import unittest
from unittest import mock

from core import skills
from tests.helpers import scratch_dir


def _skill(text, name="some-skill", root=None):
    """Write `text` as <root>/<name>/SKILL.md and return its path."""
    d = (root or scratch_dir()) / name
    d.mkdir(parents=True)
    p = d / "SKILL.md"
    p.write_text(text)
    return p


def _description(text):
    return skills._parse_skill_md(_skill(text))["description"]


class SkillDescriptionTests(unittest.TestCase):
    def test_description_comes_from_the_frontmatter(self):
        text = ("---\nname: some-skill\ndescription: Use when the user asks for a widget.\n"
                "---\n\n# Widget Maker\n\nBody.\n")
        self.assertEqual(_description(text), "Use when the user asks for a widget.")

    def test_double_quoted_description_loses_its_quotes_and_escapes(self):
        text = ('---\nname: some-skill\ndescription: "Check every \\"claim\\" — then '
                'report"\n---\n\n# Checker\n')
        self.assertEqual(_description(text), 'Check every "claim" — then report')

    def test_single_quoted_description_loses_its_quotes(self):
        text = "---\nname: some-skill\ndescription: 'It''s a tool'\n---\n"
        self.assertEqual(_description(text), "It's a tool")

    def test_folded_description_is_one_line(self):
        text = ("---\nname: some-skill\ndescription: >-\n  Use when delegating a task,\n"
                "  or resuming one.\nuser-invocable: true\n---\n\n# Delegate\n")
        self.assertEqual(_description(text), "Use when delegating a task, or resuming one.")

    def test_quoted_description_on_the_lines_below_the_key(self):
        text = ('---\nname: some-skill\ndescription:\n  "Solve the problem: then\n'
                '  verify it."\nlicense: MIT\n---\n')
        self.assertEqual(_description(text), "Solve the problem: then verify it.")

    def test_first_heading_when_the_frontmatter_has_no_description(self):
        text = "---\nname: some-skill\n---\n\nIntro line.\n\n# Widget Maker\n"
        self.assertEqual(_description(text), "Widget Maker")

    def test_first_heading_when_there_is_no_frontmatter(self):
        text = "# Widget Maker\n\nMakes widgets.\n\nUse when the user asks for one.\n"
        self.assertEqual(_description(text), "Widget Maker")

    def test_directory_name_when_there_is_neither(self):
        self.assertEqual(_description("Just some text.\n"), "some-skill")

    def test_card_carries_no_trigger_field(self):
        # Nothing read it, and it held whichever line first said "use when".
        info = skills._parse_skill_md(_skill("---\ndescription: Use when asked.\n---\n"))
        self.assertEqual(set(info), {"name", "description", "path"})


class ListAllSkillsTests(unittest.TestCase):
    def test_claude_and_codex_skills_are_listed_with_their_descriptions(self):
        claude_dir, codex_dir = scratch_dir(), scratch_dir()
        _skill("---\ndescription: Use when making widgets.\n---\n# Widgets\n",
               "widgets", claude_dir)
        _skill("---\ndescription: >-\n  Installs\n  skills.\n---\n",
               "installer", codex_dir / ".system")
        with mock.patch.object(skills, "SKILLS_DIR", claude_dir), \
                mock.patch.object(skills, "CODEX_SKILLS_DIR", codex_dir):
            listed = {s["name"]: (s["origin"], s["description"])
                      for s in skills.list_all_skills()}
        self.assertEqual(listed, {"widgets": ("claude", "Use when making widgets."),
                                  "installer": ("codex-system", "Installs skills.")})


if __name__ == "__main__":
    unittest.main()

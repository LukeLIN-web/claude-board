"""session_activity skill_contents: what a Skill call loaded, read off the meta row
that names the call — even when the call is far behind the timeline's tail."""
import unittest

from core import transcripts
from tests.helpers import assistant_row, user_row, write_jsonl


def _skill(cid: str, name: str) -> dict:
    row = assistant_row("")
    row["message"]["content"] = [{"type": "tool_use", "id": cid, "name": "Skill",
                                  "input": {"skill": name}}]
    return row


def _loaded(cid: str, text: str, **kw) -> dict:
    return user_row(text, isMeta=True, sourceToolUseID=cid, **kw)


class SkillContentsTests(unittest.TestCase):
    def test_body_of_each_skill_is_its_first_invocations_meta_row(self):
        f = write_jsonl([
            _skill("a", "design"), user_row("Launching skill: design"),
            _loaded("a", "Design body", blocks=True),
            _skill("b", "review"), _loaded("b", "Review body"),
            _skill("c", "design"), _loaded("c", "Design body, again"),
        ])
        self.assertEqual(transcripts.session_activity(f)["skill_contents"],
                         {"design": "Design body", "review": "Review body"})

    def test_a_call_that_loaded_nothing_has_no_body(self):
        # A denied call: its tool_result is the error and no meta row follows,
        # so a later meta row for some other call is not taken for it.
        f = write_jsonl([
            _skill("a", "update-config"), user_row("Error: denied"),
            _loaded("z", "unrelated"),
        ])
        self.assertEqual(transcripts.session_activity(f)["skill_contents"], {})
        self.assertEqual(transcripts.session_activity(f)["skills_used"], ["update-config"])


if __name__ == "__main__":
    unittest.main()

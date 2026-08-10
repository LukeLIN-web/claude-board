"""The recap Claude writes when you've been away belongs on the timeline.

Claude logs a recap as a `system` row with subtype `away_summary`, and every
`system` row used to flatten to one placeholder the client hides — so the board
dropped exactly the summary written for someone who wasn't watching. These tests
pin the recap to its own kind, and pin the other `system` subtypes (a
turn_duration or stop_hook_summary lands after nearly every turn) to staying out.
"""
import json
import tempfile
import unittest
from pathlib import Path

from core import transcripts
from core.textcap import GOAL_CHARS


def _write(rows) -> Path:
    d = Path(tempfile.mkdtemp())
    p = d / "t.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return p


def _recap(ts, content):
    return {"type": "system", "subtype": "away_summary",
            "timestamp": ts, "content": content}


class RecapTests(unittest.TestCase):
    def test_recap_becomes_its_own_timeline_row(self):
        text = ("Goal: evaluate the pi0.5 checkpoint on 14 RoboTwin tasks. "
                "Next: relaunch on nnmc64's four stable GPUs.")
        evs = transcripts.timeline(_write([_recap("2026-08-10T05:45:32Z", text)]))
        self.assertEqual([e["kind"] for e in evs], ["recap"])
        self.assertEqual(evs[0]["text"], text)
        self.assertEqual(evs[0]["ts"], "2026-08-10T05:45:32Z")
        self.assertEqual(evs[0]["role"], "system")

    def test_the_config_hint_is_dropped(self):
        # A TUI affordance appended to the recap. The board has no /config, so on
        # a card it is one line of noise repeated on every recap.
        evs = transcripts.timeline(_write([
            _recap("2026-08-10T05:45:32Z", "Ran the suite. (disable recaps in /config)"),
        ]))
        self.assertEqual(evs[0]["text"], "Ran the suite.")

    def test_recap_without_the_hint_is_untouched(self):
        evs = transcripts.timeline(_write([
            _recap("2026-08-10T05:45:32Z", "Ran the suite (twice) and it passed."),
        ]))
        self.assertEqual(evs[0]["text"], "Ran the suite (twice) and it passed.")

    def test_empty_recap_makes_no_row(self):
        evs = transcripts.timeline(_write([
            _recap("2026-08-10T05:45:32Z", "  "),
            _recap("2026-08-10T05:46:00Z", "(disable recaps in /config)"),
        ]))
        self.assertEqual(evs, [])

    def test_other_system_subtypes_stay_hidden(self):
        # These land after nearly every turn; as recaps they would bury the real
        # ones. They keep the placeholder `system` kind the client hides.
        evs = transcripts.timeline(_write([
            {"type": "system", "subtype": "turn_duration",
             "timestamp": "2026-08-10T05:45:00Z", "content": "42s"},
            {"type": "system", "subtype": "stop_hook_summary",
             "timestamp": "2026-08-10T05:45:01Z", "content": "hook output"},
        ]))
        self.assertEqual({e["kind"] for e in evs}, {"system"})

    def test_recap_sits_between_the_turns_it_summarizes(self):
        evs = transcripts.timeline(_write([
            {"type": "user", "timestamp": "2026-08-10T05:40:00Z",
             "message": {"content": [{"type": "text", "text": "go"}]}},
            _recap("2026-08-10T05:45:00Z", "Working on it. Next: your call on GPUs."),
            {"type": "assistant", "timestamp": "2026-08-10T05:50:00Z",
             "message": {"model": "claude-opus-5",
                         "content": [{"type": "text", "text": "done"}]}},
        ]))
        self.assertEqual([e["kind"] for e in evs],
                         ["user_text", "recap", "assistant_text"])

class SessionGoalTests(unittest.TestCase):
    """The goal is pinned above the timeline, so a wrong one stays wrong on
    screen for the rest of the session. Every phrasing here is one Claude
    actually wrote in a recap."""

    def _goal(self, *contents):
        rows = [_recap(f"2026-08-10T0{i}:00:00Z", c) for i, c in enumerate(contents)]
        return transcripts.session_goal(_write(rows))

    def test_colon_form(self):
        g = self._goal("Goal: run RoboTwin evaluation of the pi0.5 checkpoint "
                       "across 14 tasks. Environment, weights and assets are ready.")
        self.assertEqual(
            g["text"],
            "run RoboTwin evaluation of the pi0.5 checkpoint across 14 tasks")

    def test_a_version_number_does_not_end_the_sentence(self):
        # "pi0.5" and "53.8%" both carry a period mid-goal.
        g = self._goal("Goal: evaluate the pi0.5 checkpoint at 53.8% success. "
                       "The fixed run is underway.")
        self.assertEqual(g["text"], "evaluate the pi0.5 checkpoint at 53.8% success")

    def test_is_form(self):
        g = self._goal("Goal is fine-tuning ImageWAM on Unitree G1 data. "
                       "The dataset is downloaded.")
        self.assertEqual(g["text"], "fine-tuning ImageWAM on Unitree G1 data")

    def test_semicolon_ends_the_goal(self):
        g = self._goal("Goal: fine-tune ImageWAM on Unitree G1 data; the plan "
                       "is written to plan.txt.")
        self.assertEqual(g["text"], "fine-tune ImageWAM on Unitree G1 data")

    def test_chinese_form(self):
        g = self._goal("目标：把 pi0.5 在 RoboTwin 上跑完 14 个任务。下一步等结果。")
        self.assertEqual(g["text"], "把 pi0.5 在 RoboTwin 上跑完 14 个任务")

    def test_goal_with_no_trailing_punctuation(self):
        g = self._goal("Goal: get the board shipped")
        self.assertEqual(g["text"], "get the board shipped")

    def test_the_config_hint_is_not_part_of_the_goal(self):
        g = self._goal("Goal: ship the board (disable recaps in /config)")
        self.assertEqual(g["text"], "ship the board")

    def test_newest_labelled_recap_wins(self):
        g = self._goal("Goal: the old plan. Next: something.",
                       "Goal: the new plan. Next: something else.")
        self.assertEqual(g["text"], "the new plan")
        self.assertEqual(g["ts"], "2026-08-10T01:00:00Z")

    def test_a_later_unlabelled_recap_does_not_clear_the_goal(self):
        # Claude labels the goal in about half its recaps and states it plainly
        # in the rest. Dropping the label doesn't mean the goal went away.
        g = self._goal("Goal: fine-tune ImageWAM on Unitree G1 data. Next: phase 0.",
                       "We're fine-tuning ImageWAM; phases 0-5 are done.")
        self.assertEqual(g["text"], "fine-tune ImageWAM on Unitree G1 data")

    def test_no_goal_when_no_recap_ever_labelled_one(self):
        # Better a card with no goal than one pinning a guess for the rest of
        # the session.
        self.assertIsNone(self._goal(
            "I started the claude-board server via ./run.sh; it's running detached.",
            "Comparing DMs across the two runs. Next: your call.",
        ))

    def test_source_is_named(self):
        self.assertEqual(self._goal("Goal: ship it.")["source"], "recap")

    def test_no_goal_without_a_transcript(self):
        self.assertIsNone(transcripts.session_goal("/nope/nothing.jsonl"))

    def test_goal_is_capped(self):
        long_goal = "ship " + "x" * (GOAL_CHARS + 500)
        g = self._goal("Goal: " + long_goal)
        self.assertLess(len(g["text"]), GOAL_CHARS + 60)
        # names what it held back
        self.assertIn(str(len(long_goal) - GOAL_CHARS), g["text"])


class PromptGoalTests(unittest.TestCase):
    """A prompt opened with "goal …" is how this fleet actually sets one — and
    since CLI 2.1.221 stopped writing recaps, it's the only goal a session
    running today has."""

    def _rows(self, *prompts):
        return [{"type": "user", "timestamp": f"2026-08-10T0{i}:00:00Z",
                 "message": {"content": [{"type": "text", "text": t}]}}
                for i, t in enumerate(prompts)]

    def test_a_goal_prompt_is_the_goal(self):
        g = transcripts.session_goal(_write(self._rows("goal 转写腿接 L3 做一下。")))
        self.assertEqual(g["text"], "转写腿接 L3 做一下")
        self.assertEqual(g["source"], "prompt")

    def test_only_the_first_sentence_is_pinned(self):
        # These prompts run to a whole spec; the banner gets the headline.
        g = transcripts.session_goal(_write(self._rows(
            "goal  新增 `## E11 MiniCPM 换底座跑满主表四场` + 状态板一行。要点: 12 格 = 三臂。")))
        self.assertEqual(g["text"], "新增 `## E11 MiniCPM 换底座跑满主表四场` + 状态板一行")

    def test_an_ordinary_prompt_is_not_a_goal(self):
        self.assertIsNone(transcripts.session_goal(_write(self._rows(
            "看一下这个 bug", "goals are unclear"))))  # needs the word on its own

    def test_a_queued_goal_prompt_still_counts(self):
        # Typed while the session was busy, so it never got a normal user row.
        g = transcripts.session_goal(_write([{
            "type": "attachment", "timestamp": "2026-08-10T05:00:00Z",
            "attachment": {"type": "queued_command", "prompt": "goal 把 board 发布"},
        }]))
        self.assertEqual(g["text"], "把 board 发布")

    def test_an_injected_row_cannot_set_the_goal(self):
        # A skill body that happens to open with "goal …" is not the user's goal.
        self.assertIsNone(transcripts.session_goal(_write([{
            "type": "user", "isMeta": True, "timestamp": "2026-08-10T05:00:00Z",
            "message": {"content": [{"type": "text", "text": "goal of this skill is X"}]},
        }])))

    def test_newest_source_wins_across_both_kinds(self):
        p = _write([
            _recap("2026-08-10T01:00:00Z", "Goal: the old plan. Next: something."),
            *[{"type": "user", "timestamp": "2026-08-10T02:00:00Z",
               "message": {"content": [{"type": "text", "text": "goal 新的计划"}]}}],
        ])
        g = transcripts.session_goal(p)
        self.assertEqual((g["text"], g["source"]), ("新的计划", "prompt"))

    def test_a_recap_goal_beats_an_older_goal_prompt(self):
        p = _write([
            {"type": "user", "timestamp": "2026-08-10T01:00:00Z",
             "message": {"content": [{"type": "text", "text": "goal 旧的计划"}]}},
            _recap("2026-08-10T02:00:00Z", "Goal: the newer plan. Next: something."),
        ])
        g = transcripts.session_goal(p)
        self.assertEqual((g["text"], g["source"]), ("the newer plan", "recap"))


class RecapPromptTests(unittest.TestCase):
    def test_recap_is_not_a_prompt_the_card_can_clear(self):
        # A recap is written in the first person and often ends in a question, so
        # it reads like a prompt. Counting one would clear a pending send off the
        # card that Claude never actually took.
        p = _write([_recap("2026-08-10T05:45:00Z", "Next: tell me which GPU to use.")])
        self.assertEqual(transcripts.consumed_prompt_texts(p, 0.0), [])


if __name__ == "__main__":
    unittest.main()

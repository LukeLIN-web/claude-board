"""fold_by_file: the transcript extractors read only what was appended.

Whatever order the file grew in, each answer has to be the one a single read of
the whole file gives — that read is what every other test of these extractors
pins down, so this only checks that reading in pieces never drifts from it.
"""
import json
import os
import threading
import unittest
from pathlib import Path

from core import transcripts
from tests.helpers import assistant_row, queue_op, scratch_dir, user_row, write_jsonl

EXTRACTORS = [
    (transcripts.current_model, transcripts._CurrentModel),
    (transcripts.session_loop, transcripts._SessionLoop),
    (transcripts.session_goal, transcripts._SessionGoal),
    (transcripts._consumed_prompts, transcripts._ConsumedPrompts),
    (transcripts.session_activity, transcripts._SessionActivity),
    (transcripts.extract_background_tasks, transcripts._BackgroundTasks),
    (transcripts.extract_plan_history, transcripts._PlanHistory),
]

MEM = "/home/u/.claude/projects/-p/memory"
NOTICE = ("<task-notification><task-id>t1</task-id><tool-use-id>a1</tool-use-id>"
          "<status>completed</status><summary>agent done</summary></task-notification>")


def _ts(minute: int) -> str:
    return f"2026-10-08T10:{minute:02d}:00Z"


def _tools(minute: int, *calls, model="claude-opus-4-8") -> dict:
    row = assistant_row("", _ts(minute), model=model)
    row["message"]["content"] = [{"type": "tool_use", "id": cid, "name": name, "input": inp}
                                 for cid, name, inp in calls]
    return row


def _rows() -> list[dict]:
    """A session that gives every extractor something to say, and changes its
    answer partway through."""
    return [
        user_row("goal make the board fast", _ts(0)),
        _tools(1, ("s1", "Skill", {"skill": "loop", "args": "5m check the build"}),
               ("r1", "Read", {"file_path": f"{MEM}/style.md"}),
               ("r2", "Read", {"file_path": "/home/u/.claude/skills/deploy/SKILL.md"})),
        _tools(2, ("w1", "Write", {"file_path": f"{MEM}/notes.md", "content": "remember"}),
               ("p1", "Write", {"file_path": "/home/u/.claude/plans/fold.md", "content": "# v1"}),
               ("a1", "Agent", {"description": "review", "prompt": "look"}),
               ("b1", "Bash", {"command": "sleep 9", "run_in_background": True,
                               "description": "wait"})),
        {"type": "system", "subtype": "away_summary", "timestamp": _ts(3),
         "content": "Goal: keep every card current; the plan is written."},
        queue_op("enqueue", NOTICE, _ts(4)),
        queue_op("remove", "run the tests", _ts(5)),
        _tools(6, ("e1", "Edit", {"file_path": f"{MEM}/notes.md", "old_string": "remember",
                                  "new_string": "remember this"}),
               ("p2", "Edit", {"file_path": "/home/u/.claude/plans/fold.md",
                               "old_string": "# v1", "new_string": "# v1.1"}),
               ("x1", "Bash", {"command": "cat ~/.claude/skills/deploy/run.sh"}),
               ("c1", "CronCreate", {"recurring": True, "cron": "*/30 * * * *",
                                     "prompt": "check the build"}),
               model="claude-fable-5"),
        user_row("<command-name>/loop</command-name><command-args>10m tidy up</command-args>", _ts(7)),
        user_row(NOTICE, _ts(8)),
        _tools(9, ("w2", "ScheduleWakeup", {"prompt": "/loop keep going", "delaySeconds": 600})),
        queue_op("dequeue", None, _ts(10)),
        assistant_row("done", _ts(11), model="claude-sonnet-4-6"),
    ]


def _line(row: dict) -> str:
    return json.dumps(row) + "\n"


def _full_read(make, path) -> object:
    """The answer one pass over the whole file gives."""
    fold = make()
    for d in transcripts._iter_lines(Path(path)):
        fold.step(d)
    return fold.result()


class FoldTests(unittest.TestCase):
    def _assert_all_match(self, path, rows_so_far):
        for extract, make in EXTRACTORS:
            with self.subTest(extractor=make.__name__, rows=rows_so_far):
                self.assertEqual(extract(path), _full_read(make, path))

    def test_the_fixture_changes_every_answer_as_it_grows(self):
        # Otherwise the checks below could pass on a file read only once.
        rows = _rows()
        first = write_jsonl(rows[:2])
        whole = write_jsonl(rows)
        for _, make in EXTRACTORS:
            with self.subTest(extractor=make.__name__):
                self.assertNotEqual(_full_read(make, first), _full_read(make, whole))

    def test_a_file_appended_to_in_pieces_reads_as_a_whole(self):
        rows = _rows()
        p = write_jsonl(rows[:1])
        done = 1
        self._assert_all_match(p, done)
        for n in (3, 4, 7, 8, len(rows)):
            with p.open("a") as f:
                f.write("".join(_line(r) for r in rows[done:n]))
            done = n
            self._assert_all_match(p, done)

    def test_a_row_still_being_written_waits_for_its_newline(self):
        rows = _rows()
        p = write_jsonl(rows[:6])
        line = _line(rows[6])
        with p.open("a") as f:
            f.write(line[:40])
        whole = write_jsonl(rows[:6])
        for extract, make in EXTRACTORS:
            with self.subTest(extractor=make.__name__):
                self.assertEqual(extract(p), _full_read(make, whole))
        with p.open("a") as f:
            f.write(line[40:])
        self._assert_all_match(p, 7)

    def test_a_file_rewritten_in_place_is_read_again(self):
        # Same inode, longer content, and the same first row: only the bytes
        # where the last read stopped tell the two files apart.
        rows = _rows()
        p = write_jsonl(rows[:5])
        ino = os.stat(p).st_ino
        self._assert_all_match(p, 5)
        changed = rows[:2] + [assistant_row("other", _ts(3), model="claude-haiku-4-5")] + rows[5:]
        write_jsonl(changed, p)
        self.assertEqual(os.stat(p).st_ino, ino)
        self._assert_all_match(p, len(changed))

    def test_a_rewrite_that_only_changes_the_first_row_is_read_again(self):
        rows = _rows()
        p = write_jsonl(rows[:5])
        self._assert_all_match(p, 5)
        first = user_row("goal something else entirely!!", _ts(0))
        write_jsonl([first] + rows[1:], p)
        self._assert_all_match(p, len(rows))

    def test_a_shrunk_file_is_read_again(self):
        rows = _rows()
        p = write_jsonl(rows)
        self._assert_all_match(p, len(rows))
        write_jsonl(rows[:3], p)
        self._assert_all_match(p, 3)

    def test_a_replaced_file_is_read_again(self):
        rows = _rows()
        p = write_jsonl(rows[:5])
        self._assert_all_match(p, 5)
        tmp = write_jsonl(rows[5:])
        os.replace(tmp, p)
        self._assert_all_match(p, len(rows) - 5)

    def test_a_missing_file_has_the_empty_answer(self):
        p = scratch_dir() / "gone.jsonl"
        for extract, make in EXTRACTORS:
            with self.subTest(extractor=make.__name__):
                self.assertEqual(extract(p), make().result())
        write_jsonl(_rows(), p)
        self._assert_all_match(p, len(_rows()))
        p.unlink()
        for extract, make in EXTRACTORS:
            with self.subTest(extractor=make.__name__, deleted=True):
                self.assertEqual(extract(p), make().result())

    def test_an_answer_handed_out_is_not_changed_by_later_rows(self):
        rows = _rows()
        p = write_jsonl(rows[:3])
        before = {make.__name__: extract(p) for extract, make in EXTRACTORS}
        frozen = json.dumps(before, sort_keys=True)
        with p.open("a") as f:
            f.write("".join(_line(r) for r in rows[3:]))
        for extract, _ in EXTRACTORS:
            extract(p)
        self.assertEqual(json.dumps(before, sort_keys=True), frozen)

    def test_callers_on_one_file_read_it_once(self):
        steps = []

        class Counting:
            def step(self, d):
                steps.append(d)

            def result(self):
                return len(steps)

        count = transcripts.fold_by_file(Counting)
        p = write_jsonl([user_row(str(i)) for i in range(2000)])
        start = threading.Barrier(4)

        def call():
            start.wait()
            count(p)

        threads = [threading.Thread(target=call) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(steps), 2000)


if __name__ == "__main__":
    unittest.main()

"""Async background work (transcripts.extract_background_tasks) and the triage
call built on it (core.patrol).

The card's triage answers one question for someone scanning the board: does this
session need me? Two states are easy to confuse and expensive to mix up — a
session waiting on work that is still running (leave it alone) and one holding a
finished task's notification it never picked up (go type something). Both look
identical from the outside: quiet session, no output, status says busy.
"""
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from core import patrol, transcripts


def _write(rows) -> Path:
    d = Path(tempfile.mkdtemp())
    p = d / "t.jsonl"
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    return p


def _launch(tid, name, inp):
    return {"type": "assistant", "timestamp": "2026-09-08T20:40:00Z",
            "message": {"stop_reason": "tool_use",
                        "content": [{"type": "tool_use", "id": tid,
                                     "name": name, "input": inp}]}}


def _end_turn(text):
    return {"type": "assistant", "timestamp": "2026-09-08T20:53:31Z",
            "message": {"stop_reason": "end_turn",
                        "content": [{"type": "text", "text": text}]}}


def _notification(task_id, tool_use_id="", status="completed", summary="Agent finished"):
    tool = f"<tool-use-id>{tool_use_id}</tool-use-id>\n" if tool_use_id else ""
    return (f"<task-notification>\n<task-id>{task_id}</task-id>\n{tool}"
            f"<status>{status}</status>\n<summary>{summary}</summary>\n</task-notification>")


def _queue(op, body, ts="2026-09-08T21:00:33Z"):
    return {"type": "queue-operation", "operation": op, "timestamp": ts, "content": body}


def _delivered(body, ts="2026-09-08T21:16:11Z"):
    """The notification arriving as a user turn — the session took it."""
    return {"type": "user", "timestamp": ts, "message": {"content": body}}


class BackgroundLedgerTests(unittest.TestCase):
    """extract_background_tasks: launch → completion notification → delivery."""

    def tearDown(self):
        shutil.rmtree(self.p.parent, ignore_errors=True)

    def _tasks(self, rows):
        self.p = _write(rows)
        return transcripts.extract_background_tasks(self.p)

    def test_a_launch_with_no_notification_is_still_running(self):
        got = self._tasks([
            _launch("t1", "Agent", {"description": "W6 facts", "prompt": "go"}),
            _end_turn("挂上了"),
        ])
        self.assertEqual([(t["type"], t["state"]) for t in got], [("agent", "running")])

    def test_a_launch_is_running_even_though_its_tool_result_came_back(self):
        # The trap this module was built on: an async launch answers immediately
        # ("Async agent launched successfully") and works on afterwards, so
        # "tool_use without tool_result" — the old test — matched nothing at all.
        got = self._tasks([
            _launch("t1", "Agent", {"description": "W6 facts"}),
            {"type": "user", "timestamp": "2026-09-08T20:40:01Z",
             "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                      "content": "Async agent launched successfully."}]}},
            _end_turn("挂上了"),
        ])
        self.assertEqual([t["state"] for t in got], ["running"])

    def test_completion_the_session_never_took_is_undelivered(self):
        got = self._tasks([
            _launch("t1", "Agent", {"description": "W6 facts: prompts"}),
            _end_turn("只剩这个子代理"),
            _queue("enqueue", _notification("task1", "t1")),
        ])
        self.assertEqual([(t["state"], t["description"]) for t in got],
                         [("undelivered", "W6 facts: prompts")])
        self.assertEqual(got[0]["ts"], transcripts._parse_ts("2026-09-08T21:00:33Z"))

    def test_completion_removed_from_the_queue_is_settled(self):
        # `remove` is the row that says Claude took the item — the opposite of
        # the item still waiting, though both are `queue-operation` rows.
        got = self._tasks([
            _launch("t1", "Agent", {"description": "W6 facts"}),
            _end_turn("等着"),
            _queue("enqueue", _notification("task1", "t1")),
            _queue("remove", _notification("task1", "t1"), ts="2026-09-08T21:00:34Z"),
        ])
        self.assertEqual(got, [])

    def test_completion_delivered_as_a_user_turn_is_settled(self):
        got = self._tasks([
            _launch("t1", "Agent", {"description": "W6 facts"}),
            _end_turn("等着"),
            _queue("enqueue", _notification("task1", "t1")),
            _delivered(_notification("task1", "t1")),
        ])
        self.assertEqual(got, [])

    def test_the_last_notification_settles_by_task_id_alone(self):
        # A task reports twice and the final one carries no <tool-use-id>, so
        # <task-id> has to be the identity or the two never pair up.
        got = self._tasks([
            _launch("t1", "Agent", {"description": "W6 facts"}),
            _end_turn("等着"),
            _queue("enqueue", _notification("task1", "t1")),
            _queue("enqueue", _notification("task1"), ts="2026-09-08T21:12:35Z"),
            _delivered(_notification("task1")),
        ])
        self.assertEqual(got, [])

    def test_a_progress_event_is_not_a_completion(self):
        got = self._tasks([
            _launch("t1", "Monitor", {"description": "watch the eval", "persistent": True}),
            _end_turn("盯着"),
            _queue("enqueue", _notification("task1", "t1", status="running")),
        ])
        self.assertEqual([t["state"] for t in got], ["running"])

    def test_a_flush_settles_what_it_cannot_name(self):
        # `dequeue` carries no content. It shows up alongside real deliveries, so
        # it is read as one: a missed stall is quieter than a false alarm.
        got = self._tasks([
            _launch("t1", "Agent", {"description": "W6 facts"}),
            _end_turn("等着"),
            _queue("enqueue", _notification("task1", "t1")),
            {"type": "queue-operation", "operation": "dequeue",
             "timestamp": "2026-09-08T21:16:11Z", "content": None},
        ])
        self.assertEqual(got, [])

    def test_an_undelivered_completion_with_no_launch_still_reports(self):
        # Nothing to name it with but its own summary — still worth surfacing.
        got = self._tasks([
            _end_turn("在等"),
            _queue("enqueue", _notification("task1", summary="Agent \"scan\" finished")),
        ])
        self.assertEqual([(t["state"], t["description"]) for t in got],
                         [("undelivered", 'Agent "scan" finished')])

    def test_foreground_work_is_not_tracked(self):
        got = self._tasks([
            _launch("t1", "Bash", {"command": "sleep 600"}),
            _end_turn("跑着"),
        ])
        self.assertEqual(got, [])

    def test_each_kind_of_async_launch_is_tracked(self):
        got = self._tasks([
            _launch("t1", "Bash", {"command": "train.sh", "run_in_background": True}),
            _launch("t2", "Monitor", {"command": "tail log", "persistent": True}),
            _launch("t3", "Agent", {"description": "scan the courts"}),
            _end_turn("三件"),
        ])
        self.assertEqual({t["type"] for t in got}, {"bash_bg", "monitor", "agent"})


class ClassifyTests(unittest.TestCase):
    """patrol.classify — background_tasks is supplied by app.py before the call."""

    def tearDown(self):
        shutil.rmtree(self.p.parent, ignore_errors=True)

    def _classify(self, rows, tasks=(), **over):
        self.p = _write(rows)
        w = {"status": "idle", "idle_seconds": 900, "transcript_path": str(self.p),
             "background_tasks": list(tasks)}
        w.update(over)
        return patrol.classify(w)

    def _stuck(self, age, **over):
        return {"type": "agent", "description": "W6 facts: prompts", "command": "",
                "state": "undelivered", "ts": time.time() - age, **over}

    def test_an_undelivered_completion_needs_a_person(self):
        got = self._classify([_end_turn("等子代理")],
                             tasks=[self._stuck(15 * 60)])
        self.assertEqual(got["triage"], "stalled")
        self.assertEqual(got["suggestion"], "去终端敲一下")
        self.assertIn("W6 facts: prompts", got["reason"])

    def test_it_outranks_the_session_calling_itself_busy(self):
        # The reason this went unseen: a session holding an undelivered
        # notification keeps reporting `busy`, and the busy shortcut returned
        # "working" before anything looked at the queue.
        got = self._classify([_end_turn("等子代理")],
                             tasks=[self._stuck(15 * 60)],
                             status="busy", idle_seconds=10)
        self.assertEqual(got["triage"], "stalled")

    def test_a_handover_in_flight_is_not_a_stall(self):
        # Delivery normally takes milliseconds; a snapshot must not catch one
        # mid-flight and cry stall.
        got = self._classify([_end_turn("等子代理")],
                             tasks=[self._stuck(2)])
        self.assertEqual(got["triage"], "working")

    def test_work_still_running_is_working(self):
        got = self._classify([_end_turn("挂上了")], tasks=[
            {"type": "bash_bg", "description": "train.sh", "command": "",
             "state": "running", "ts": 0.0}])
        self.assertEqual(got["triage"], "working")
        self.assertIn("train.sh", got["reason"])

    def test_nothing_in_flight_is_finished(self):
        got = self._classify([_end_turn("写完了,等你看")])
        self.assertEqual(got["triage"], "completed")
        self.assertEqual(got["suggestion"], "建议 review")

    def test_prose_about_background_work_does_not_make_a_session_busy(self):
        # There is no keyword fallback any more: a session that merely mentions
        # 后台/monitor is judged by what it actually has in flight, which is
        # nothing.
        got = self._classify([_end_turn("后台那批 monitor 我已经全收掉了,没有在跑的东西")])
        self.assertEqual(got["triage"], "completed")

    def test_a_session_stopped_mid_tool_still_needs_a_person(self):
        got = self._classify([_launch("t1", "Bash", {"command": "pytest"})])
        self.assertEqual(got["triage"], "stalled")
        self.assertEqual(got["suggestion"], "需要用户介入")

    def test_background_work_outranks_a_long_idle(self):
        # Idle past the closeable threshold with a subagent still out: the card
        # must not offer to close it.
        got = self._classify([_end_turn("等它回来")], idle_seconds=7200, tasks=[
            {"type": "agent", "description": "long scan", "command": "",
             "state": "running", "ts": 0.0}])
        self.assertEqual(got["triage"], "working")


if __name__ == "__main__":
    unittest.main()

"""Tests for oh-my-humanize (omh) cards: process detection, the tty breadcrumb
that names a session's transcript, turn status, the timeline, and reading the
boxed composer omh draws instead of a ❯ prompt.
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from core import omh, tmux

# `omh` execs bun on the CLI entry through an un-normalized `scripts/../src` path
# (copied from `ps` on a live session).
OMH_ARGS = ("bun --preload /home/u/.local/share/omh/source/main/packages/coding-agent/"
            "scripts/omp.ts /home/u/.local/share/omh/source/main/packages/coding-agent/"
            "scripts/../src/cli.ts")

SESSION_LINES = [
    {"type": "session", "version": 3, "id": "01a0", "cwd": "/tmp/proj",
     "timestamp": "2026-10-01T10:00:00.000Z"},
    {"type": "model_change", "id": "m1", "parentId": None,
     "timestamp": "2026-10-01T10:00:01.000Z", "model": "anthropic/claude-opus-5"},
    {"type": "message", "id": "a1", "parentId": "m1", "timestamp": "2026-10-01T10:00:02.000Z",
     "message": {"role": "user", "content": "orchestrate 把 todo.md 里的事拆给小弟做",
                 "timestamp": 1}},
    {"type": "message", "id": "a2", "parentId": "a1", "timestamp": "2026-10-01T10:00:03.000Z",
     "message": {"role": "assistant", "model": "claude-opus-5", "stopReason": "toolUse",
                 "content": [{"type": "thinking", "thinking": "plan"},
                             {"type": "text", "text": "先读 todo.md\n再派活"},
                             {"type": "toolCall", "id": "t1", "name": "read",
                              "arguments": {"path": "todo.md"}}],
                 "timestamp": 2}},
    {"type": "message", "id": "a3", "parentId": "a2", "timestamp": "2026-10-01T10:00:04.000Z",
     "message": {"role": "toolResult", "toolCallId": "t1", "toolName": "read",
                 "content": [{"type": "text", "text": "- item A\n- item B"}],
                 "isError": False, "timestamp": 3}},
    {"type": "message", "id": "a4", "parentId": "a3", "timestamp": "2026-10-01T10:00:05.000Z",
     "message": {"role": "assistant", "model": "claude-opus-5", "stopReason": "stop",
                 "content": [{"type": "text", "text": "两件事都派完了"}], "timestamp": 4}},
]


def _msgs(lines):
    return omh._messages(lines)


class TestDetection(unittest.TestCase):
    def test_interactive_tui(self):
        self.assertTrue(omh._is_interactive_omh(OMH_ARGS))
        self.assertTrue(omh._is_interactive_omh(OMH_ARGS + " --continue"))

    def test_headless_runs_are_not_cards(self):
        for extra in (" -p hi", " --print hi", " --mode=rpc", " --mode json", " --export x.jsonl"):
            self.assertFalse(omh._is_interactive_omh(OMH_ARGS + extra), extra)

    def test_other_processes(self):
        self.assertFalse(omh._is_interactive_omh("bun run build:native"))
        self.assertFalse(omh._is_interactive_omh("/home/u/.local/bin/claude --dangerously-skip-permissions"))


class TestBreadcrumb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig = omh.BREADCRUMB_DIR
        omh.BREADCRUMB_DIR = Path(self.tmp.name)

    def tearDown(self):
        omh.BREADCRUMB_DIR = self.orig
        self.tmp.cleanup()

    def test_reads_session_path_for_tty(self):
        (Path(self.tmp.name) / "pts-115").write_text(
            "/tmp/proj\n/home/u/.omp/agent/sessions/-tmp-proj/2026-10-02T00-35-32-779Z_01a0.jsonl\n")
        p = omh._breadcrumb_session("pts/115")
        self.assertEqual(p.name, "2026-10-02T00-35-32-779Z_01a0.jsonl")
        self.assertEqual(p.stem.split("_", 1)[-1], "01a0")

    def test_missing_breadcrumb(self):
        self.assertIsNone(omh._breadcrumb_session("pts/9"))


class TestStatus(unittest.TestCase):
    OLD = time.time() - 60

    def test_finished_turn_is_idle(self):
        self.assertEqual(omh._status(_msgs(SESSION_LINES), self.OLD), "idle")

    def test_tool_call_pending_is_busy(self):
        self.assertEqual(omh._status(_msgs(SESSION_LINES[:4]), self.OLD), "busy")

    def test_tool_result_or_prompt_last_is_busy(self):
        self.assertEqual(omh._status(_msgs(SESSION_LINES[:5]), self.OLD), "busy")
        self.assertEqual(omh._status(_msgs(SESSION_LINES[:3]), self.OLD), "busy")

    def test_fresh_write_is_busy(self):
        self.assertEqual(omh._status(_msgs(SESSION_LINES), time.time()), "busy")

    def test_current_task_is_latest_reply(self):
        self.assertEqual(omh._current_task(_msgs(SESSION_LINES)), "两件事都派完了")


class TestTranscript(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w") as f:
            for d in SESSION_LINES:
                f.write(json.dumps(d, ensure_ascii=False) + "\n")

    def tearDown(self):
        os.unlink(self.path)

    def test_first_user_input(self):
        self.assertEqual(omh._first_user_input(Path(self.path)),
                         "orchestrate 把 todo.md 里的事拆给小弟做")

    def test_timeline(self):
        ev = omh.omh_timeline(self.path)
        self.assertEqual([e["kind"] for e in ev],
                         ["user_text", "assistant_text", "tool_use", "tool_result", "assistant_text"])
        self.assertEqual(ev[2]["tool"], "read")
        self.assertEqual(json.loads(ev[2]["extra"]["arguments"]), {"path": "todo.md"})
        self.assertEqual(ev[3]["text"], "- item A\n- item B")


# Bottom of a live omh pane (160 cols), empty and holding a wrapped prompt.
BOX_EMPTY = (
    " Warning: No models available. Use /login or set an API key environment variable.\n"
    "\n"
    "╭── π  ┤ ⬢ no-model ┤ 🗑 …/scratchpad/omhprobe ┤ ◫ 25K/? ⟲ ┤──────────────────\n"
    "╰─                                                                          ─╯\n"
)
BOX_WRAPPED = (
    "╭── π  ┤ ⬢ no-model ┤ 🗑 …/scratchpad/omhprobe ┤ ◫ 25K/? ⟲ ┤──────────────────\n"
    "│  word000 word001 word002                                                 │\n"
    "│  word003 word004 word005                                                 │\n"
    "╰─ word006 word007                                                         ─╯\n"
)


class TestOmhComposer(unittest.TestCase):
    def test_empty_box(self):
        self.assertEqual(tmux._composer_text(BOX_EMPTY, tmux.OMH_COMPOSER), "")

    def test_wrapped_rows(self):
        self.assertEqual(tmux._composer_text(BOX_WRAPPED, tmux.OMH_COMPOSER),
                         "word000 word001 word002\nword003 word004 word005\nword006 word007")

    def test_no_box_means_no_composer(self):
        self.assertIsNone(tmux._composer_text("$ ls\n", tmux.OMH_COMPOSER))

    def test_tail_check_sees_wrapped_prompt(self):
        orig = tmux.capture_pane
        tmux.capture_pane = lambda pane, scrollback=0: {"ok": True, "text": BOX_WRAPPED}
        try:
            self.assertTrue(tmux._composer_has_tail("%1", "word005 word006 word007", tmux.OMH_COMPOSER))
            self.assertFalse(tmux._composer_has_tail("%1", "something else", tmux.OMH_COMPOSER))
        finally:
            tmux.capture_pane = orig


if __name__ == "__main__":
    unittest.main()

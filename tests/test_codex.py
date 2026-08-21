"""Tests for Codex rollout parsing — specifically that a card/timeline shows the
*user's* prompt, not the assistant's first reply or a synthetic injection.

Real Codex rollouts log the user's submitted prompt as a clean
`event_msg`/`user_message` record, plus a `response_item` message (role=user)
carrying `input_text`. The latter shape is also reused for synthetic injections
(`<environment_context>`, `<subagent_notification>`, …), which must be skipped.
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from core import codex


# A minimal rollout mirroring the on-disk event ordering of a real session:
# developer prompt, a synthetic <environment_context> user turn, the REAL user
# prompt (both as response_item/input_text and as event_msg/user_message), then
# the assistant's first reply. Mirrors the bug repro exactly.
ROLLOUT_LINES = [
    {"type": "session_meta", "payload": {"id": "abc", "cwd": "/tmp/proj",
                                         "timestamp": "2026-06-11T14:50:58Z"}},
    {"type": "response_item", "payload": {"type": "message", "role": "developer",
        "content": [{"type": "input_text", "text": "You are Codex."}]}},
    {"type": "response_item", "payload": {"type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "<environment_context>\n  <cwd>/tmp/proj</cwd>\n</environment_context>"}]}},
    {"type": "response_item", "payload": {"type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "检查一下训练代码有没有问题."}]}},
    {"type": "event_msg", "payload": {"type": "user_message",
        "message": "检查一下训练代码有没有问题.", "images": []}},
    {"type": "event_msg", "payload": {"type": "agent_message",
        "message": "我会按代码审查处理…"}},
    {"type": "response_item", "payload": {"type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "我会按代码审查处理…"}]}},
]

REAL_PROMPT = "检查一下训练代码有没有问题."
ASSISTANT_REPLY = "我会按代码审查处理…"


def _write_rollout(lines):
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False, encoding="utf-8")
    for d in lines:
        tmp.write(json.dumps(d, ensure_ascii=False) + "\n")
    tmp.close()
    return Path(tmp.name)


class TestExtractFirstUserInput(unittest.TestCase):
    def setUp(self):
        self.path = _write_rollout(ROLLOUT_LINES)

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_returns_real_user_prompt_not_assistant_reply(self):
        self.assertEqual(codex._extract_first_user_input(self.path), REAL_PROMPT)

    def test_skips_synthetic_environment_context(self):
        out = codex._extract_first_user_input(self.path)
        self.assertNotIn("environment_context", out)

    def test_falls_back_to_assistant_when_no_user_text(self):
        no_user = [l for l in ROLLOUT_LINES
                   if not (l["type"] == "event_msg" and l["payload"].get("type") == "user_message")
                   and not (l["type"] == "response_item" and l["payload"].get("role") == "user")]
        p = _write_rollout(no_user)
        try:
            self.assertEqual(codex._extract_first_user_input(p), ASSISTANT_REPLY)
        finally:
            p.unlink(missing_ok=True)


class TestCodexTimeline(unittest.TestCase):
    def setUp(self):
        self.path = _write_rollout(ROLLOUT_LINES)

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_timeline_includes_user_prompt(self):
        evs = codex.codex_timeline(self.path)
        user_texts = [e["text"] for e in evs if e["kind"] == "user_text"]
        self.assertIn(REAL_PROMPT, user_texts)

    def test_timeline_user_prompt_not_duplicated(self):
        evs = codex.codex_timeline(self.path)
        user_texts = [e["text"] for e in evs if e["kind"] == "user_text"]
        self.assertEqual(user_texts.count(REAL_PROMPT), 1)


# The same session as Codex writes it since ~CLI 0.147: no `user_message` event
# at all — every turn is an `item_completed` thread item — and shell work runs
# through the freeform `exec` tool (`custom_tool_call`), whose result comes back
# as a list of parts rather than a string. The opening role=user record bundles
# the project's AGENTS.md with `<environment_context>`; nobody typed either half.
ITEM_ROLLOUT_LINES = [
    {"type": "session_meta", "payload": {"id": "abc", "cwd": "/tmp/proj",
                                         "timestamp": "2026-08-20T12:53:26Z"}},
    {"type": "response_item", "payload": {"type": "message", "role": "developer",
        "content": [{"type": "input_text", "text": "You are Codex."}]}},
    {"type": "response_item", "payload": {"type": "message", "role": "user",
        "content": [
            {"type": "input_text", "text": "# AGENTS.md instructions for /tmp/proj\n\n<INSTRUCTIONS>\n…"},
            {"type": "input_text", "text": "<environment_context>\n  <cwd>/tmp/proj</cwd>\n</environment_context>"},
        ]}},
    {"type": "response_item", "payload": {"type": "message", "role": "user",
        "content": [{"type": "input_text", "text": REAL_PROMPT}]}},
    {"type": "event_msg", "payload": {"type": "item_completed", "item": {
        "type": "UserMessage", "id": "u1",
        "content": [{"type": "text", "text": REAL_PROMPT}]}}},
    {"type": "event_msg", "payload": {"type": "item_completed", "item": {
        "type": "CommandExecution", "id": "exec-1", "command": ["/bin/bash", "-lc", "ls"]}}},
    {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec",
        "call_id": "call_1",
        "input": 'const r = await tools.exec_command({"cmd":"ls train/"});'}},
    {"type": "response_item", "payload": {"type": "custom_tool_call_output",
        "call_id": "call_1", "output": [
            {"type": "input_text", "text": "Script completed"},
            {"type": "input_text", "text": "train.py"},
        ]}},
    {"type": "response_item", "payload": {"type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": ASSISTANT_REPLY}]}},
]


class TestThreadItemRollout(unittest.TestCase):
    """Newer rollouts drop `user_message`; the prompt lives in a UserMessage item."""

    def setUp(self):
        self.path = _write_rollout(ITEM_ROLLOUT_LINES)

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_first_input_is_the_typed_prompt_not_the_agents_md_preamble(self):
        self.assertEqual(codex._extract_first_user_input(self.path), REAL_PROMPT)

    def test_timeline_shows_the_typed_prompt_once(self):
        user_texts = [e["text"] for e in codex.codex_timeline(self.path)
                      if e["kind"] == "user_text"]
        self.assertEqual(user_texts, [REAL_PROMPT])

    def test_timeline_shows_the_custom_exec_tool_call(self):
        calls = [e for e in codex.codex_timeline(self.path) if e["kind"] == "tool_use"]
        self.assertEqual([e["tool"] for e in calls], ["exec"])
        self.assertIn("ls train/", calls[0]["extra"]["arguments"])

    def test_tool_result_parts_are_joined_into_text(self):
        results = [e for e in codex.codex_timeline(self.path) if e["kind"] == "tool_result"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["text"], "Script completed\ntrain.py")

    def test_prompt_not_doubled_when_a_rollout_carries_both_shapes(self):
        both = list(ITEM_ROLLOUT_LINES)
        both.insert(5, {"type": "event_msg", "payload": {
            "type": "user_message", "message": REAL_PROMPT, "images": []}})
        p = _write_rollout(both)
        try:
            user_texts = [e["text"] for e in codex.codex_timeline(p)
                          if e["kind"] == "user_text"]
            self.assertEqual(user_texts, [REAL_PROMPT])
        finally:
            p.unlink(missing_ok=True)

    def test_pending_custom_tool_call_reads_as_busy(self):
        # Rollout ends on an issued `exec` with no output yet, and was last
        # written long enough ago that the mtime shortcut can't answer.
        pending = ITEM_ROLLOUT_LINES[:-2]
        p = _write_rollout(pending)
        try:
            self.assertEqual(
                codex._infer_codex_status(p, mtime=time.time() - 600), "busy")
        finally:
            p.unlink(missing_ok=True)

    def test_goal_injection_with_attributes_is_not_a_prompt(self):
        injected = [l for l in ITEM_ROLLOUT_LINES
                    if not (l["type"] == "event_msg"
                            and l["payload"].get("type") == "item_completed")]
        injected[3] = {"type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text",
                "text": '<codex_internal_context source="goal">\nContinue.\n</codex_internal_context>'}]}}
        p = _write_rollout(injected)
        try:
            self.assertEqual(codex._extract_first_user_input(p), ASSISTANT_REPLY)
        finally:
            p.unlink(missing_ok=True)


# A rollout straddling a /clear: an old prompt+reply, then a new prompt+reply.
# Every line carries a top-level timestamp, as real Codex rollouts do.
CLEAR_ROLLOUT = [
    {"timestamp": "2026-06-11T10:00:00Z", "type": "event_msg",
     "payload": {"type": "user_message", "message": "OLD prompt before clear"}},
    {"timestamp": "2026-06-11T10:00:05Z", "type": "response_item",
     "payload": {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "OLD assistant reply"}]}},
    {"timestamp": "2026-06-11T12:00:00Z", "type": "event_msg",
     "payload": {"type": "user_message", "message": "NEW prompt after clear"}},
    {"timestamp": "2026-06-11T12:00:05Z", "type": "response_item",
     "payload": {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "NEW assistant reply"}]}},
]
CLEAR_CUTOFF_MS = codex._parse_iso_ms("2026-06-11T11:00:00Z")  # between old and new


class TestClearHidesPreClearEvents(unittest.TestCase):
    """Codex /clear leaves the rollout intact, so the card filters events older
    than the clear time (see codex.mark_cleared / cleared_at_ms)."""

    def setUp(self):
        self.path = _write_rollout(CLEAR_ROLLOUT)

    def tearDown(self):
        self.path.unlink(missing_ok=True)
        codex._cleared_at_ms.clear()

    def test_first_input_skips_pre_clear_prompt(self):
        self.assertEqual(
            codex._extract_first_user_input(self.path, since_ms=CLEAR_CUTOFF_MS),
            "NEW prompt after clear")

    def test_first_input_without_cutoff_shows_old(self):
        self.assertEqual(
            codex._extract_first_user_input(self.path),
            "OLD prompt before clear")

    def test_timeline_drops_pre_clear_events(self):
        evs = codex.codex_timeline(self.path, since_ms=CLEAR_CUTOFF_MS)
        texts = [e["text"] for e in evs]
        self.assertNotIn("OLD prompt before clear", texts)
        self.assertNotIn("OLD assistant reply", texts)
        self.assertIn("NEW prompt after clear", texts)

    def test_last_assistant_text_skips_pre_clear(self):
        self.assertEqual(
            codex._last_assistant_text(self.path, since_ms=CLEAR_CUTOFF_MS),
            "NEW assistant reply")

    def test_unparseable_timestamp_is_not_hidden(self):
        # A line we can't date should be shown rather than silently dropped.
        self.assertFalse(codex._before_clear("", CLEAR_CUTOFF_MS))
        self.assertFalse(codex._before_clear("not-a-date", CLEAR_CUTOFF_MS))

    def test_mark_cleared_roundtrip(self):
        self.assertEqual(codex.cleared_at_ms(99999), 0)
        codex.mark_cleared(99999)
        self.assertGreater(codex.cleared_at_ms(99999), 0)


class TestRolloutFdSelection(unittest.TestCase):
    """A codex TUI that holds several rollout fds must resolve to the live one.

    Repro: a turn ran to completion (frozen rollout) and the session continued
    into a new rollout. Both fds stay open; picking the older one latches the
    card onto a dead transcript so it never updates.
    """

    def _fake_fd_dir(self, marker: str, *, frozen_newer: bool):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        sessions = tmp / marker.lstrip("/")
        sessions.mkdir(parents=True)
        frozen = sessions / "rollout-2026-06-14T16-48-38-019ec889.jsonl"
        live = sessions / "rollout-2026-06-14T17-13-32-019ec8a0.jsonl"
        frozen.write_text("{}\n")
        live.write_text("{}\n")
        # Live rollout is the more recently written one (unless we invert it to
        # prove selection is by mtime, not by name/listdir order).
        os.utime(frozen, (2000, 2000) if frozen_newer else (1000, 1000))
        os.utime(live, (1000, 1000) if frozen_newer else (2000, 2000))
        fd_dir = tmp / "fd"
        fd_dir.mkdir()
        # listdir order is arbitrary on /proc; name the symlinks so the frozen
        # one sorts first, the exact case that used to win.
        os.symlink(frozen, fd_dir / "50")
        os.symlink(live, fd_dir / "53")
        return str(fd_dir), str(frozen), str(live), str(sessions)

    def test_picks_newest_rollout_when_multiple_fds_open(self):
        fd_dir, frozen, live, marker = self._fake_fd_dir("codex-sessions", frozen_newer=False)
        self.assertEqual(codex._newest_rollout_in_fd_dir(fd_dir, marker), live)

    def test_selection_is_by_mtime_not_listdir_order(self):
        # Invert mtimes: the lexically-first fd is now the newest → must win.
        fd_dir, frozen, live, marker = self._fake_fd_dir("codex-sessions", frozen_newer=True)
        self.assertEqual(codex._newest_rollout_in_fd_dir(fd_dir, marker), frozen)

    def test_no_rollout_fds_returns_none(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        fd_dir = tmp / "fd"
        fd_dir.mkdir()
        other = tmp / "some.log"
        other.write_text("x")
        os.symlink(other, fd_dir / "3")
        self.assertIsNone(codex._newest_rollout_in_fd_dir(str(fd_dir), "codex-sessions"))

    def test_missing_fd_dir_returns_none(self):
        self.assertIsNone(codex._newest_rollout_in_fd_dir("/proc/0/fd", "codex-sessions"))


# Codex wraps API failures in task_complete.error.message as a JSON blob; the
# human-readable text sits at error.message inside it. Repro: session 019f9feb,
# where every turn 400'd on an unsupported model and the card showed nothing.
_MODEL_ERR = ("{\"type\":\"error\",\"status\":400,\"error\":{\"type\":\"invalid_request_error\","
              "\"message\":\"The 'gpt-5.6-sol' model is not supported when using Codex with a ChatGPT account.\"}}")

def _task_complete(ts, error_msg=None):
    err = {"message": error_msg, "codex_error_info": "other"} if error_msg else None
    return {"timestamp": ts, "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "t", "error": err}}


class TestLastTurnError(unittest.TestCase):
    """The card must surface the latest turn's error — and only while it IS the
    latest turn outcome; a later successful turn clears it."""

    def _err_of(self, lines, since_ms=0):
        p = _write_rollout(lines)
        try:
            return codex._last_turn_error(p, since_ms)
        finally:
            p.unlink(missing_ok=True)

    def test_surfaces_latest_turn_error_message(self):
        out = self._err_of([_task_complete("2026-07-26T19:36:10Z", _MODEL_ERR)])
        self.assertEqual(
            out, "The 'gpt-5.6-sol' model is not supported when using Codex with a ChatGPT account.")

    def test_later_successful_turn_clears_error(self):
        out = self._err_of([
            _task_complete("2026-07-26T19:36:10Z", _MODEL_ERR),
            _task_complete("2026-07-26T19:40:00Z"),
        ])
        self.assertIsNone(out)

    def test_no_task_complete_means_no_error(self):
        self.assertIsNone(self._err_of(ROLLOUT_LINES))

    def test_pre_clear_error_is_hidden(self):
        cutoff = codex._parse_iso_ms("2026-07-26T19:38:00Z")
        out = self._err_of([_task_complete("2026-07-26T19:36:10Z", _MODEL_ERR)],
                           since_ms=cutoff)
        self.assertIsNone(out)

    def test_unparseable_error_message_shown_raw(self):
        out = self._err_of([_task_complete("2026-07-26T19:36:10Z", "stream disconnected")])
        self.assertEqual(out, "stream disconnected")


class TestSubagentRolloutIsNotTheCard(unittest.TestCase):
    """`spawn_agent` opens a child thread's rollout from the SAME process.

    Repro: while the subagent runs its rollout is the newest fd, so newest-by-
    mtime swapped the card onto that near-empty thread — the user's whole
    history vanished mid-turn and came back only when the subagent finished.
    The user thread must win regardless of mtime.
    """

    def _meta(self, sid, parent=None):
        payload = {"session_id": parent or sid, "id": sid, "cwd": "/tmp/proj",
                   "timestamp": "2026-07-31T18:10:18Z"}
        if parent:
            payload["parent_thread_id"] = parent
            payload["source"] = {"subagent": {"thread_spawn": {"agent": "worker"}}}
        else:
            payload["source"] = "cli"
            payload["thread_source"] = "user"
        return {"timestamp": "2026-07-31T18:10:18Z", "type": "session_meta",
                "payload": payload}

    def _fd_dir(self, *, subagent_newer=True, include_user=True):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        sessions = tmp / "codex-sessions"
        sessions.mkdir(parents=True)
        user = sessions / "rollout-2026-07-30T20-56-35-019fb651.jsonl"
        sub = sessions / "rollout-2026-07-31T11-30-40-019fb971.jsonl"
        user.write_text(json.dumps(self._meta("019fb651")) + "\n")
        sub.write_text(json.dumps(self._meta("019fb971", parent="019fb651")) + "\n")
        os.utime(user, (1000, 1000) if subagent_newer else (2000, 2000))
        os.utime(sub, (2000, 2000) if subagent_newer else (1000, 1000))
        fd_dir = tmp / "fd"
        fd_dir.mkdir()
        if include_user:
            os.symlink(user, fd_dir / "50")
        os.symlink(sub, fd_dir / "53")
        return str(fd_dir), str(user), str(sub), str(sessions)

    def test_user_thread_wins_while_subagent_is_the_newest_fd(self):
        fd_dir, user, sub, marker = self._fd_dir(subagent_newer=True)
        self.assertEqual(codex._newest_rollout_in_fd_dir(fd_dir, marker), user)

    def test_user_thread_still_wins_when_it_is_also_newest(self):
        fd_dir, user, sub, marker = self._fd_dir(subagent_newer=False)
        self.assertEqual(codex._newest_rollout_in_fd_dir(fd_dir, marker), user)

    def test_subagent_only_process_still_resolves(self):
        # A standalone subagent process holds no user thread — better to card it
        # than to card nothing.
        fd_dir, user, sub, marker = self._fd_dir(include_user=False)
        self.assertEqual(codex._newest_rollout_in_fd_dir(fd_dir, marker), sub)

    def test_classifier(self):
        _, user, sub, _ = self._fd_dir()
        self.assertFalse(codex._is_subagent_rollout(user))
        self.assertTrue(codex._is_subagent_rollout(sub))

    def test_unreadable_meta_is_treated_as_user_thread(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        p = tmp / "rollout-2026-07-31T00-00-00-deadbeef.jsonl"
        p.write_text("not json\n")
        self.assertFalse(codex._is_subagent_rollout(str(p)))


if __name__ == "__main__":
    unittest.main()

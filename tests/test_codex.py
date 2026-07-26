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


if __name__ == "__main__":
    unittest.main()

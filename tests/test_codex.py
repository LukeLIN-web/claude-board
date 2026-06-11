"""Tests for Codex rollout parsing — specifically that a card/timeline shows the
*user's* prompt, not the assistant's first reply or a synthetic injection.

Real Codex rollouts log the user's submitted prompt as a clean
`event_msg`/`user_message` record, plus a `response_item` message (role=user)
carrying `input_text`. The latter shape is also reused for synthetic injections
(`<environment_context>`, `<subagent_notification>`, …), which must be skipped.
"""
import json
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


if __name__ == "__main__":
    unittest.main()

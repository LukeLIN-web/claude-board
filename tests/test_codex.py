"""Tests for Codex rollout parsing — specifically that a card/timeline shows the
*user's* prompt, not the assistant's first reply or a synthetic injection.

Real Codex rollouts log the user's submitted prompt as a clean
`event_msg`/`user_message` record, plus a `response_item` message (role=user)
carrying `input_text`. The latter shape is also reused for synthetic injections
(`<environment_context>`, `<subagent_notification>`, …), which must be skipped.
"""
import json
import os
import time
import unittest
from unittest import mock

from core import codex, sessions, transcripts
from tests.helpers import scratch_dir, write_jsonl


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
    # As Codex writes it: non-ASCII text as raw UTF-8, not \u escapes.
    return write_jsonl(lines, ensure_ascii=False)


class TestExtractFirstUserInput(unittest.TestCase):
    def setUp(self):
        self.path = _write_rollout(ROLLOUT_LINES)

    def test_returns_real_user_prompt_not_assistant_reply(self):
        # Equal to the typed prompt, so also not the synthetic
        # <environment_context> turn logged ahead of it.
        self.assertEqual(codex._extract_first_user_input(self.path), REAL_PROMPT)

    def test_falls_back_to_assistant_when_no_user_text(self):
        no_user = [l for l in ROLLOUT_LINES
                   if not (l["type"] == "event_msg" and l["payload"].get("type") == "user_message")
                   and not (l["type"] == "response_item" and l["payload"].get("role") == "user")]
        self.assertEqual(codex._extract_first_user_input(_write_rollout(no_user)),
                         ASSISTANT_REPLY)


class TestCodexTimeline(unittest.TestCase):
    def test_timeline_user_prompt_not_duplicated(self):
        evs = codex.codex_timeline(_write_rollout(ROLLOUT_LINES))
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
        user_texts = [e["text"] for e in codex.codex_timeline(_write_rollout(both))
                      if e["kind"] == "user_text"]
        self.assertEqual(user_texts, [REAL_PROMPT])

    def test_pending_custom_tool_call_reads_as_busy(self):
        # Rollout ends on an issued `exec` with no output yet, and was last
        # written long enough ago that the mtime shortcut can't answer.
        p = _write_rollout(ITEM_ROLLOUT_LINES[:-2])
        self.assertEqual(
            codex._infer_codex_status(codex._read_tail_events(p),
                                      mtime=time.time() - 600), "busy")

    def test_goal_injection_with_attributes_is_not_a_prompt(self):
        injected = [l for l in ITEM_ROLLOUT_LINES
                    if not (l["type"] == "event_msg"
                            and l["payload"].get("type") == "item_completed")]
        injected[3] = {"type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text",
                "text": '<codex_internal_context source="goal">\nContinue.\n</codex_internal_context>'}]}}
        self.assertEqual(codex._extract_first_user_input(_write_rollout(injected)),
                         ASSISTANT_REPLY)


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
CLEAR_CUTOFF_MS = int(transcripts._parse_ts("2026-06-11T11:00:00Z") * 1000)  # between old and new


class TestClearHidesPreClearEvents(unittest.TestCase):
    """Codex /clear leaves the rollout intact, so the card filters events older
    than the clear time (see codex.mark_cleared / cleared_at_ms)."""

    def setUp(self):
        self.path = _write_rollout(CLEAR_ROLLOUT)

    def tearDown(self):
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
            codex._last_assistant_text(codex._read_tail_events(self.path),
                                       since_ms=CLEAR_CUTOFF_MS),
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
        tmp = scratch_dir()
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
        tmp = scratch_dir()
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
        return codex._last_turn_error(codex._read_tail_events(_write_rollout(lines)), since_ms)

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
        cutoff = int(transcripts._parse_ts("2026-07-26T19:38:00Z") * 1000)
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
        tmp = scratch_dir()
        sessions = tmp / "codex-sessions"
        sessions.mkdir(parents=True)
        user = sessions / "rollout-2026-07-30T20-56-35-019fb651.jsonl"
        sub = sessions / "rollout-2026-07-31T11-30-40-019fb971.jsonl"
        write_jsonl([self._meta("019fb651")], user)
        write_jsonl([self._meta("019fb971", parent="019fb651")], sub)
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
        p = scratch_dir() / "rollout-2026-07-31T00-00-00-deadbeef.jsonl"
        p.write_text("not json\n")
        self.assertFalse(codex._is_subagent_rollout(str(p)))


class TestRolloutsParsedOncePerChange(unittest.TestCase):
    """History lists every rollout every 30s; one that hasn't changed is not
    read again, and one that has gone is forgotten."""

    def test_list_codex_sessions_reuses_unchanged_rollouts(self):
        root = scratch_dir()
        self.addCleanup(codex._clear_caches)
        a, b = root / "rollout-a.jsonl", root / "rollout-b.jsonl"
        for f in (a, b):
            write_jsonl(ROLLOUT_LINES, f)
        with mock.patch.object(codex, "CODEX_SESSIONS_DIR", root), \
                mock.patch.object(codex, "_scan_activity",
                                  wraps=codex._scan_activity) as scan:
            self.assertEqual(len(codex.list_codex_sessions()), 2)
            self.assertEqual(len(codex.list_codex_sessions()), 2)
            self.assertEqual(scan.call_count, 2)
            with a.open("a") as f:
                f.write(json.dumps({"type": "turn_context", "payload": {"model": "m"}}) + "\n")
            b.unlink()
            (s,) = codex.list_codex_sessions()
            self.assertEqual((s["transcript_path"], s["model"]), (str(a), "m"))
            self.assertEqual(scan.call_count, 3)
        self.assertEqual(list(codex._session_cache), [a])


def _exec(cmd):
    return {"type": "response_item", "payload": {
        "type": "function_call", "name": "exec_command", "arguments": json.dumps({"cmd": cmd})}}


class TestMemoryActivity(unittest.TestCase):
    """A Codex session's memory reads and writes are counted per memory, as
    Claude's and OpenCode's are, so the Memory page's reverse lookup finds it."""

    MEM = "/home/u/.claude/projects/-home-u-proj/memory"

    def setUp(self):
        self.path = _write_rollout(ROLLOUT_LINES + [
            _exec(f"cat {self.MEM}/notes.md"),
            _exec(f"sed -n 1,5p {self.MEM}/notes.md {self.MEM}/MEMORY.md"),
            _exec(f"echo more >> {self.MEM}/notes.md"),
        ])

    def test_reads_and_writes_are_counted(self):
        act = codex.extract_codex_session_activity(self.path)
        self.assertEqual(act["memory_breakdown"], {
            "per_memory_reads": {"notes": 2}, "per_memory_writes": {"notes": 1},
            "per_memory_edits": {}})
        self.assertEqual(act["memory_ops"], [{"name": "notes", "operation": "read"},
                                             {"name": "notes", "operation": "write"}])

    def test_the_reverse_lookup_finds_the_session(self):
        from core import history
        s = history.HistorySession(**codex._codex_session(self.path, self.path.stat()))
        with mock.patch.object(history, "index", return_value=[s]):
            got = history.sessions_touching("notes", "memory_breakdown", history.MEMORY_KINDS)
        self.assertEqual([(r["session_id"], r["reads"], r["writes"]) for r in got["sessions"]],
                         [("abc", 2, 1)])


class TestTurnContextModel(unittest.TestCase):
    """The model + reasoning effort a session ran on come from turn_context; the
    last one wins, since /model rewrites both mid-session."""

    def test_last_turn_context_wins(self):
        act = codex.extract_codex_session_activity(_write_rollout(ROLLOUT_LINES + [
            {"type": "turn_context", "payload": {"model": "gpt-6-astra", "effort": "medium"}},
            {"type": "turn_context", "payload": {"model": "gpt-5.6-sol", "effort": "high"}},
        ]))
        self.assertEqual((act["model"], act["effort"]), ("gpt-5.6-sol", "high"))

    def test_no_turn_context_means_blank(self):
        act = codex.extract_codex_session_activity(_write_rollout(ROLLOUT_LINES))
        self.assertEqual((act["model"], act["effort"]), ("", ""))


# The VS Code / Cursor extension's app server, as `ps -eo args` prints it.
EDITOR_APP_SERVER = ("/home/u/.cursor-server/extensions/openai.chatgpt-26.908.40401-linux-x64/"
                     "bin/linux-x86_64/codex -c features.code_mode_host=true app-server "
                     "--analytics-default-enabled")


class TestIsInteractiveCodex(unittest.TestCase):
    """Only the first word that is neither an option nor an option's value can
    name a subcommand; past it is the opening prompt."""

    def test_a_config_value_is_not_the_subcommand(self):
        self.assertFalse(codex._is_interactive_codex("codex -c features.x=true app-server"))
        self.assertFalse(codex._is_interactive_codex(EDITOR_APP_SERVER))
        # A value with a space in it only stays whole in the real argv.
        self.assertFalse(codex._is_interactive_codex(
            ["codex", "-c", "instructions=do this", "app-server"]))

    def test_value_options_are_stepped_over(self):
        self.assertFalse(codex._is_interactive_codex("codex -m gpt-5 exec fix the tests"))
        self.assertTrue(codex._is_interactive_codex(["codex", "-m", "gpt-5", "fix the tests"]))
        self.assertFalse(codex._is_interactive_codex(
            "codex --model gpt-5 --sandbox read-only --cd /w exec x"))

    def test_a_value_written_into_its_option_is_one_word(self):
        self.assertFalse(codex._is_interactive_codex("codex --model=gpt-5 exec x"))
        self.assertTrue(codex._is_interactive_codex("codex --model=gpt-5 fix it"))
        self.assertFalse(codex._is_interactive_codex("codex -mgpt-5 exec x"))

    def test_background_subcommands_are_not_interactive(self):
        for sub in ("exec", "mcp-server", "app-server"):
            self.assertFalse(codex._is_interactive_codex(f"codex {sub}"), sub)
        self.assertFalse(codex._is_interactive_codex("node /n/bin/codex exec do it"))

    def test_a_tui_is_interactive(self):
        for args in ("codex", "codex --yolo", "codex resume --last",
                     "node /n/bin/codex --yolo"):
            self.assertTrue(codex._is_interactive_codex(args), args)

    def test_a_prompt_that_names_a_subcommand_is_interactive(self):
        self.assertTrue(codex._is_interactive_codex("codex please exec the tests"))
        self.assertTrue(codex._is_interactive_codex(["codex", "exec the migration plan"]))
        self.assertTrue(codex._is_interactive_codex(
            ["codex", "-m", "gpt-5", "app-server is down, find out why"]))

    def test_not_codex(self):
        self.assertFalse(codex._is_interactive_codex("vim codex.py"))
        self.assertFalse(codex._is_interactive_codex("python codex_helper.py"))


@unittest.skipUnless(os.path.isdir("/proc"), "Codex discovery reads /proc")
class TestCodexCardsFromProcesses(unittest.TestCase):
    """Which codex processes on a terminal get a card, read off the real argv."""

    def setUp(self):
        # No cwd filter, whatever the environment running the suite sets.
        env = mock.patch.dict(os.environ, {"CLAUDE_FLEET_CWD_INCLUDE": "",
                                           "CLAUDE_FLEET_CWD_EXCLUDE": ""})
        env.start()
        sessions._reload_cwd_filters()
        self.addCleanup(lambda: (env.stop(), sessions._reload_cwd_filters()))

    def _carded(self, rows, argv=None):
        """The pids carded from `rows` of (pid, tty, ps args); `argv` maps a pid
        to its real argv, which is the ps args for any other."""
        table = {pid: sessions.Proc(1, "Sl+", tty, "codex", args) for pid, tty, args in rows}
        argv = argv or {}
        with mock.patch.object(codex, "proc_table", return_value=table), \
             mock.patch.object(codex, "_proc_argv", create=True,
                               side_effect=lambda pid, args: argv.get(pid, args)), \
             mock.patch.object(codex, "_pid_alive", return_value=True), \
             mock.patch.object(codex, "_proc_start_ms", return_value=0), \
             mock.patch.object(codex, "_rollout_fd", return_value=None), \
             mock.patch.object(codex.os, "readlink", return_value="/tmp/proj"):
            return {w.pid for w in codex.list_codex_windows()}

    def test_an_editor_app_server_on_a_terminal_gets_no_card(self):
        # Only its having no tty kept it off the board.
        self.assertEqual(self._carded([(800, "pts/5", EDITOR_APP_SERVER),
                                       (801, "pts/6", "codex")]), {801})

    def test_a_prompt_beginning_with_a_subcommand_is_read_off_the_real_argv(self):
        # ps prints `codex "exec the plan"` as `codex exec the plan`.
        self.assertEqual(self._carded([(802, "pts/7", "codex exec the plan"),
                                       (803, "pts/8", "codex exec the plan")],
                                      argv={802: ["codex", "exec the plan"]}), {802})

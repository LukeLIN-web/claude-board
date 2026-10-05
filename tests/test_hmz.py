"""Tests for humanize (hmz) cards: telling the interface from hmz's headless
processes, finding the newest run of a directory, and reading a run's epic and
the sessions it keeps.
"""
import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from core import hmz

# Copied from `ps` while a flow ran: the interface, the headless run, and the
# sandbox plumbing under one of its turns.
TUI = "/home/u/miniconda3/bin/python /home/u/miniconda3/bin/hmz"
EXEC = "/home/u/miniconda3/bin/python /home/u/miniconda3/bin/hmz exec -f aot -a writer=claude/x"
FENCE = '/home/u/miniconda3/bin/python -Pm hmz internal fence --policy={"read":["/usr"]}'

RUN = [
    {"event": "began", "at": "2026-10-02T00:50:30.513Z", "flow": "commander_delegate",
     "task": "split todo.md across workers\nmore detail",
     "agents": [{"agent": "commander", "backend": "claude", "model": "claude-opus-5-5"},
                {"agent": "worker", "backend": "claude", "model": "claude-opus-5-5"}]},
    {"event": "opened", "at": "2026-10-02T00:50:45.034Z", "agent": "commander",
     "backend": "claude", "session": "4b21"},
    {"event": "opened", "at": "2026-10-02T00:52:00.000Z", "agent": "worker",
     "backend": "claude", "session": "9c03"},
]
ENDED = RUN + [{"event": "ended", "at": "2026-10-02T01:10:00.000Z", "how": "done"}]


class TestDetection(unittest.TestCase):
    def test_interface(self):
        self.assertTrue(hmz._is_interactive_hmz(TUI))
        self.assertTrue(hmz._is_interactive_hmz("hmz"))

    def test_headless_and_plumbing_are_not_cards(self):
        self.assertFalse(hmz._is_interactive_hmz(EXEC))
        self.assertFalse(hmz._is_interactive_hmz(FENCE))

    def test_other_processes(self):
        self.assertFalse(hmz._is_interactive_hmz("humanize-plugin-mcp"))
        self.assertFalse(hmz._is_interactive_hmz("/home/u/.local/bin/claude --print"))


class TestEpics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig = hmz.HMZ_HOME
        hmz.HMZ_HOME = Path(self.tmp.name)

    def tearDown(self):
        hmz.HMZ_HOME = self.orig
        self.tmp.cleanup()

    def _run(self, cwd_slug, name, events):
        d = Path(self.tmp.name) / "epics" / cwd_slug / name
        d.mkdir(parents=True)
        (d / "epic.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
        return d / "epic.jsonl"

    def test_newest_run_of_the_directory(self):
        self._run("-home-u-tmp-hmz-aot", "20261001T090000.000Z-aaaaaa", ENDED)
        newest = self._run("-home-u-tmp-hmz-aot", "20261002T005030.513Z-a779c6", RUN)
        self._run("-home-u-other", "20261003T000000.000Z-bbbbbb", RUN)
        self.assertEqual(hmz._latest_epic("/home/u/tmp/hmz-aot"), newest)

    def test_dir_without_runs(self):
        self.assertIsNone(hmz._latest_epic("/home/u/never.ran"))

    def test_home_before_and_after_the_rename(self):
        # hmz moved ~/.humanize to ~/.hmz; one from before the move keeps the old name.
        base = Path(self.tmp.name)
        with mock.patch.object(hmz, "HMZ_HOME", base / ".hmz"), \
                mock.patch.object(hmz, "HMZ_HOME_WAS", base / ".humanize"):
            (base / ".humanize").mkdir()
            self.assertEqual(hmz._default_home(), base / ".humanize")
            (base / ".humanize").rename(base / ".hmz")
            self.assertEqual(hmz._default_home(), base / ".hmz")
            # Both there: the new one is in use, the old one left as it was.
            (base / ".humanize").mkdir()
            self.assertEqual(hmz._default_home(), base / ".hmz")

    def test_humanize_home_of_the_process(self):
        # An hmz started with HUMANIZE_HOME keeps its runs there, not in ~/.humanize.
        other = Path(self.tmp.name) / "elsewhere"
        d = other / "epics" / "-home-u-x" / "20261002T000000.000Z-cccccc"
        d.mkdir(parents=True)
        (d / "epic.jsonl").write_text(json.dumps(RUN[0]) + "\n")
        self.assertIsNone(hmz._latest_epic("/home/u/x"))
        self.assertEqual(hmz._latest_epic("/home/u/x", other), d / "epic.jsonl")


class TestPromptTaken(unittest.TestCase):
    """A send to hmz succeeds on hmz's own record of the line, never on an
    emptied composer."""

    CWD = "/home/u/proj"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.history = Path(self.tmp.name) / "history.jsonl"
        home = mock.patch.object(hmz, "_home", return_value=Path(self.tmp.name))
        home.start()
        self.addCleanup(home.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _say(self, text, workdir=CWD):
        with self.history.open("a") as f:
            f.write(json.dumps({"at": "2026-10-02T20:43:25Z", "workdir": workdir,
                                "text": text}) + "\n")

    def test_taken_once_hmz_writes_the_line_down(self):
        self._say("an earlier task")
        taken = hmz.prompt_taken(1, self.CWD, "split   the\ntodo", "%1")
        self.assertFalse(taken())
        self._say("split the todo")  # whitespace differs only
        self.assertTrue(taken())

    def test_lines_from_before_the_send_do_not_count(self):
        self._say("split the todo")
        self._say("something else")
        taken = hmz.prompt_taken(1, self.CWD, "split the todo", "%1")
        self.assertFalse(taken())

    def test_no_history_yet(self):
        taken = hmz.prompt_taken(1, self.CWD, "first ever", "%1")
        self.assertFalse(taken())
        self._say("first ever")
        self.assertTrue(taken())

    def test_repeat_of_the_last_line_falls_back_to_the_screen(self):
        # hmz doesn't write a repeat of the line it was last given, so the
        # history can't confirm it; the echo above the composer has to.
        self._say("again", workdir="/elsewhere")
        self._say("again")
        taken = hmz.prompt_taken(1, self.CWD, "again", "%1")
        with mock.patch.object(hmz.tmux, "_shown_above_composer",
                               return_value=True) as shown:
            self.assertTrue(taken())
        shown.assert_called_once_with("%1", "again")


class TestRefusal(unittest.TestCase):
    """hmz writes a line down before reading it, so a line it refuses is taken;
    the refusal is only on its screen, under the echo of the line."""

    TYPED = "$parallel_flame_chase , bear, 帮我把 codeworld   - qwen 上 分数提高, 越高越好"

    # Copied from the pane after the live miss: the echo wraps, the refusal is
    # the next line, and the composer's own chrome sits under it.
    def _screen(self, after_echo):
        return "\n".join([
            "    The agent flow system for token maxxing.",
            "",
            "❯ $parallel_flame_chase , bear, 帮我把 codeworld   - qwen 上 分数提高,",
            "越高越好",
            *after_echo,
            "",
            "",
            "                                        assistant · claude/claude-opus-5-5:high",
            "─" * 60,
            "❯ ",
            "─" * 60,
            "  ◉ chat · /home/u/robot         ← monitor · ctrl+c exit",
        ])

    def _refusal(self, screen, text=TYPED):
        with mock.patch.object(hmz.tmux, "capture_pane", return_value={"ok": True, "text": screen}):
            return hmz.refusal("%1", text)

    def test_the_red_line_under_the_echo(self):
        self.assertEqual(self._refusal(self._screen(["hmz: no such flow: parallel_flame_chase"])),
                         "hmz: no such flow: parallel_flame_chase")

    def test_a_wrapped_refusal_is_read_whole(self):
        self.assertEqual(
            self._refusal(self._screen(["hmz: /monitor is only available on a run, not on",
                                        "the chat transcript"])),
            "hmz: /monitor is only available on a run, not on the chat transcript")

    def test_nothing_said_yet(self):
        # The composer's chrome under the echo is not hmz answering.
        self.assertEqual(self._refusal(self._screen([])), "")

    def test_what_follows_is_not_a_refusal(self):
        self.assertEqual(self._refusal(self._screen(["● writer is working"])), "")

    def test_a_refusal_of_an_earlier_line_is_not_this_ones(self):
        screen = "\n".join(["❯ $nosuch x", "hmz: no such flow: nosuch", ""]) + "\n" + self._screen([])
        self.assertEqual(self._refusal(screen), "")

    def test_no_echo(self):
        self.assertEqual(self._refusal(self._screen(["hmz: no such flow: x"]), "never typed"), "")


class TestMenu(unittest.TestCase):
    """A `$flow` its directory hasn't set up opens that flow's setup menu and
    holds the line: no run, and only the terminal says why."""

    # Copied from the pane while the live run waited on its setup.
    SETUP = "\n".join([
        "",
        "  hmz › parallel_flame_chase                                 ● unsaved changes",
        "  Configure each role: an agent (CLI, account, model and effort) or an",
        "  ╭──────────────────────────────────────────────────────────────────────────╮",
        "  │ coordinator                               claude/claude-opus-5-5:high ▸  │",
        "  enter open   tab actions   esc close",
    ])
    BUDGET = ("  hmz › parallel_flame_chase › Set budget for parallel_flame_chase   ● unsaved\n"
              "  A run stops at whichever limit it reaches first; at least one limit is\n")
    CHAT = "\n".join([
        "╭─── humanize v0.1.0 ───────────────────╮",
        "│    The agent flow system for token maxxing.  │",
        "────────────────────────────────────────",
        "❯ ",
        "  ◉ chat · /home/u/proj         ← monitor · ctrl+c exit",
    ])

    def _menu(self, screen, status="idle"):
        w = TestNoRunNote._window(None)
        w.status = status
        with mock.patch.object(hmz.tmux, "pane_for_tty", return_value="%1"), \
                mock.patch.object(hmz.tmux, "capture_pane",
                                  return_value={"ok": True, "text": screen}):
            return hmz.menu(w)

    def test_the_setup_menu(self):
        self.assertEqual(self._menu(self.SETUP), "parallel_flame_chase")

    def test_a_page_inside_it(self):
        self.assertEqual(self._menu(self.BUDGET),
                         "parallel_flame_chase › Set budget for parallel_flame_chase")

    def test_the_composer_is_no_menu(self):
        self.assertEqual(self._menu(self.CHAT), "")

    def test_a_running_flow_is_not_read(self):
        self.assertEqual(self._menu(self.SETUP, status="busy"), "")

    def test_the_card_says_where_it_waits(self):
        w = TestNoRunNote._window(None)
        with mock.patch.object(hmz, "list_hmz_windows", return_value=[w]), \
                mock.patch.object(hmz.tmux, "pane_for_tty", return_value="%1"), \
                mock.patch.object(hmz.tmux, "capture_pane",
                                  return_value={"ok": True, "text": self.SETUP}):
            d = hmz.hmz_window_dicts()[0]
        self.assertEqual(d["triage"], "stalled")  # not waiting_perm: no Quick Approve "1"
        self.assertEqual(d["triage_reason"], "停在菜单：parallel_flame_chase")

    def test_the_timeline_says_so(self):
        import app
        w = TestNoRunNote._window(None)
        with mock.patch.object(app.sessions, "find_window", return_value=w), \
                mock.patch.object(hmz, "_home", return_value=Path(tempfile.gettempdir()) / "no-hmz"), \
                mock.patch.object(hmz.tmux, "pane_for_tty", return_value="%1"), \
                mock.patch.object(hmz.tmux, "capture_pane",
                                  return_value={"ok": True, "text": self.SETUP}):
            r = app.api_timeline("7")
        self.assertEqual(r["note"], hmz.MENU_NOTE.format("parallel_flame_chase"))


class TestTyped(unittest.TestCase):
    """The lines typed into one hmz, read off the history it shares with every
    other hmz of its home."""

    CWD = "/home/u/robot"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = mock.patch.object(hmz, "_home", return_value=Path(self.tmp.name))
        home.start()
        self.addCleanup(home.stop)
        _jsonl(Path(self.tmp.name) / "history.jsonl", [
            {"at": "2026-10-02T20:51:06Z", "workdir": self.CWD, "text": "before it started"},
            {"at": "2026-10-05T00:34:40Z", "workdir": "/home/u/other", "text": "another hmz"},
            {"at": "2026-10-05T00:34:42.806383Z", "workdir": self.CWD,
             "text": "$parallel_flame_chase do it"},
        ])
        self.since = int(hmz.transcripts._parse_ts("2026-10-05T00:12:16Z") * 1000)

    def test_since_it_started_in_its_directory(self):
        self.assertEqual([d["text"] for d in hmz.typed(1, self.CWD, self.since)],
                         ["$parallel_flame_chase do it"])

    def test_a_refused_line_with_no_run_is_on_the_timeline(self):
        ev = hmz.hmz_timeline(None, typed=hmz.typed(1, self.CWD, self.since))
        self.assertEqual([(e["kind"], e["text"]) for e in ev],
                         [("user_text", "$parallel_flame_chase do it")])

    def test_a_line_the_run_shows_is_not_repeated(self):
        epic = _jsonl(Path(self.tmp.name) / "run" / "epic.jsonl", [
            {"event": "began", "at": "2026-10-05T00:35:00Z", "flow": "rlar", "task": "fix  it"},
        ])
        typed = [{"at": "2026-10-05T00:34:42Z", "workdir": self.CWD, "text": "$nosuch x"},
                 {"at": "2026-10-05T00:34:59Z", "workdir": self.CWD, "text": "$rlar fix it"}]
        self.assertEqual([e["text"] for e in hmz.hmz_timeline(epic, typed=typed)],
                         ["$nosuch x", "$rlar fix  it"])


class TestNoRunNote(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = mock.patch.object(hmz, "_home", return_value=Path(self.tmp.name))
        home.start()
        self.addCleanup(home.stop)

    @staticmethod
    def _window(transcript_path):
        return hmz.Window(pid=7, session_id="hmz-7", cwd="/home/u/proj", project_name="proj",
                          project_slug="-home-u-proj", name=None, status="idle",
                          waiting_for=None, started_at=0, updated_at=0, version="",
                          tty="/dev/pts/1", transcript_path=transcript_path, alive=True,
                          hidden=False, platform="hmz")

    def test_timeline_says_why_it_is_empty(self):
        import app
        with mock.patch.object(app.sessions, "find_window", return_value=self._window(None)):
            r = app.api_timeline("7")
        self.assertEqual(r["events"], [])
        self.assertEqual(r["note"], hmz.NO_RUN_NOTE)

    def test_typed_into_but_no_run(self):
        # The live miss: hmz took `$parallel_flame_chase …`, had no such flow,
        # and the card said nothing had been typed.
        import app
        _jsonl(Path(self.tmp.name) / "history.jsonl", [
            {"at": "2026-10-05T00:34:42Z", "workdir": "/home/u/proj", "text": "$nosuch x"}])
        with mock.patch.object(app.sessions, "find_window", return_value=self._window(None)):
            r = app.api_timeline("7")
        self.assertEqual([e["text"] for e in r["events"]], ["$nosuch x"])
        self.assertEqual(r["note"], hmz.TYPED_NO_RUN_NOTE)

    def test_no_note_once_a_run_exists(self):
        import app
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write("".join(json.dumps(e) + "\n" for e in RUN))
        self.addCleanup(Path(f.name).unlink)
        with mock.patch.object(app.sessions, "find_window", return_value=self._window(f.name)):
            r = app.api_timeline("7")
        self.assertIsNone(r["note"])
        self.assertEqual(r["events"][0]["kind"], "user_text")


class TestRun(unittest.TestCase):
    def test_running_until_ended(self):
        self.assertTrue(hmz._began(RUN) and not hmz._ended(RUN))
        self.assertEqual(hmz._ended(ENDED)["how"], "done")

    def test_current_task(self):
        self.assertEqual(hmz._current_task(RUN), "commander_delegate · worker")
        self.assertEqual(hmz._current_task(ENDED), "commander_delegate done")

    def test_models_are_deduplicated(self):
        self.assertEqual(hmz._models(hmz._began(RUN)), "claude/claude-opus-5-5")

    def test_timeline(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write("".join(json.dumps(e) + "\n" for e in ENDED))
        try:
            ev = hmz.hmz_timeline(f.name)
        finally:
            Path(f.name).unlink()
        self.assertEqual([e["kind"] for e in ev],
                         ["user_text", "assistant_text", "assistant_text", "assistant_text"])
        self.assertTrue(ev[0]["text"].startswith("$commander_delegate split todo.md"))
        self.assertEqual(ev[-1]["text"], "run ended: done")


def _jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


class TestKeptSessions(unittest.TestCase):
    """What the agents said lives in the sessions the run keeps beside its epic,
    under sessions/<cli>/ laid out as that CLI lays out its home."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        run = Path(self.tmp.name) / "20261002T000000.000Z-a1b2c3"
        self.run = run
        self.epic = _jsonl(run / "epic.jsonl", [
            {"event": "began", "at": "2026-10-02T00:00:00.000Z", "flow": "rlar",
             "task": "fix   the bug"},
            # Written once the session's first turn is done, so after it.
            {"event": "opened", "at": "2026-10-02T00:00:05.000Z", "agent": "writer",
             "backend": "claude", "session": "c1", "where": "sessions/claude"},
            {"event": "called", "at": "2026-10-02T00:00:06.000Z", "flow": "review",
             "epic": "epic.review_d4e5f6.jsonl"},
            {"event": "opened", "at": "2026-10-02T00:00:09.000Z", "agent": "writer",
             "backend": "claude", "session": "c2", "parent": "c1", "where": "sessions/claude"},
            {"event": "opened", "at": "2026-10-02T00:00:10.000Z", "agent": "scout",
             "backend": "grok", "session": "g1", "where": "sessions/grok"},
        ])
        # The called flow writes its own record, and the session it opened is there.
        _jsonl(run / "epic.review_d4e5f6.jsonl", [
            {"event": "began", "at": "2026-10-02T00:00:06.000Z", "flow": "review"},
            {"event": "opened", "at": "2026-10-02T00:00:08.000Z", "agent": "reviewer",
             "backend": "codex", "session": "x9", "where": "sessions/codex"},
        ])
        first = [
            {"type": "user", "timestamp": "2026-10-02T00:00:01.000Z",
             "message": {"role": "user", "content": "fix the bug"}},
            {"type": "assistant", "timestamp": "2026-10-02T00:00:04.000Z",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "fixed it"}]}},
        ]
        claude = run / "sessions" / "claude" / "projects" / "-home-u-proj"
        self.writer_log = _jsonl(claude / "c1.jsonl", first)
        # A fork opens on a copy of the conversation it was cut from.
        _jsonl(claude / "c2.jsonl", first + [
            {"type": "user", "timestamp": "2026-10-02T00:00:08.500Z",
             "message": {"role": "user", "content": "address the review"}},
        ])
        _jsonl(run / "sessions" / "codex" / "sessions" / "2026" / "10" / "02"
               / "rollout-2026-10-02T00-00-07-x9.jsonl", [
            {"type": "event_msg", "timestamp": "2026-10-02T00:00:07.000Z",
             "payload": {"type": "user_message", "message": "review the diff"}},
            {"type": "response_item", "timestamp": "2026-10-02T00:00:07.500Z",
             "payload": {"type": "message",
                         "content": [{"type": "output_text", "text": "one nit"}]}},
        ])

    def test_every_session_of_the_run_in_order(self):
        ev = hmz.hmz_timeline(self.epic)
        self.assertEqual(
            [(e["extra"].get("agent"), e["text"]) for e in ev],
            [(None, "$rlar fix   the bug"),
             # The task handed to the first agent word for word is not shown twice.
             ("writer", "fixed it"),
             (None, "called flow review"),
             ("reviewer", "review the diff"),
             ("reviewer", "one nit"),
             # The fork's copy of c1 is shown once, its own turn after it.
             ("writer", "address the review"),
             # No log the board can read: the run's own line is all there is.
             ("scout", "scout opened a grok session")])

    def test_session_kept_in_the_cli_home(self):
        # A session that stayed where its CLI keeps it: `where` is the whole path.
        home = Path(self.tmp.name) / "dot-claude"
        _jsonl(home / "projects" / "-x" / "h1.jsonl", [
            {"type": "assistant", "timestamp": "2026-10-02T00:00:11.000Z",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "from home"}]}},
        ])
        opened = {"agent": "w", "backend": "claude", "session": "h1", "where": str(home)}
        self.assertEqual(hmz._logs(self.epic, opened), [home / "projects" / "-x" / "h1.jsonl"])

    def test_activity_is_read_off_the_session_logs(self):
        # A long turn writes its session's log, not the epic.
        for p in self.run.rglob("*.jsonl"):
            os.utime(p, (1_000, 1_000))
        os.utime(self.writer_log, (5_000, 5_000))
        self.assertEqual(hmz._last_logged(self.epic), 5_000_000)

    def test_current_task_follows_the_session_written_last(self):
        events = hmz._events(self.epic)
        for p in self.run.rglob("*.jsonl"):
            os.utime(p, (1_000, 1_000))
        codex_log = next(self.run.rglob("rollout-*.jsonl"))
        os.utime(codex_log, (2_000, 2_000))
        self.assertEqual(hmz._current_task(events, self.epic), "rlar · reviewer: one nit")
        # The writer resumes its session for the next round: no new `opened` line.
        os.utime(self.writer_log, (3_000, 3_000))
        self.assertEqual(hmz._current_task(events, self.epic), "rlar · writer: fixed it")

    def test_current_task_without_logs_is_the_last_opened(self):
        events = [e for e in hmz._events(self.epic) if e.get("event") != "opened"]
        events.append({"event": "opened", "agent": "scout", "backend": "grok", "session": "g1"})
        self.assertEqual(hmz._current_task(events), "rlar · scout")


PRICES = {"models": {
    "claude-opus-5-5": {"name": "Claude Opus 5.5", "per_million": {
        "input": 4.0, "cache_write": 5.0, "cache_read": 0.2, "output": 20.0}},
    "gpt-6-astra": {"name": "GPT-6 Astra", "per_million": {
        "input": 10.0, "cache_read": 1.0, "cache_write": 12.5, "output": 50.0}},
}}


def _claude_turn(ident, model, **usage):
    return {"type": "assistant", "timestamp": "2026-10-02T00:00:04.000Z", "requestId": "req_" + ident,
            "message": {"id": "msg_" + ident, "model": model, "role": "assistant",
                        "content": [{"type": "text", "text": "…"}], "usage": usage}}


def _codex_count(**total):
    return {"type": "event_msg", "timestamp": "2026-10-02T00:00:08.000Z",
            "payload": {"type": "token_count", "info": {"total_token_usage": total}}}


class TestSpending(unittest.TestCase):
    """What a run spent: the session logs summed and priced off hmz's copy of
    the price list while it runs, hmz's own total once it has ended."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / "prices.json").write_text(json.dumps(PRICES))
        self.run = self.home / "epics" / "-p" / "20261002T000000.000Z-a1b2c3"
        self.epic = _jsonl(self.run / "epic.jsonl", [
            {"event": "began", "at": "2026-10-02T00:00:00.000Z", "flow": "rlar", "task": "fix",
             "budget": {"duration": "PT15H", "cost": 150.0, "output_tokens": None, "graceful": True}},
            {"event": "opened", "at": "2026-10-02T00:00:05.000Z", "agent": "writer",
             "backend": "claude", "session": "c1", "where": "sessions/claude"},
            {"event": "opened", "at": "2026-10-02T00:00:08.000Z", "agent": "reviewer",
             "backend": "codex", "session": "x9", "where": "sessions/codex"},
        ])
        first = _claude_turn("1", "claude-opus-5-5", input_tokens=2, output_tokens=300,
                             cache_read_input_tokens=10_000, cache_creation_input_tokens=5_000)
        self.claude_log = _jsonl(self.run / "sessions/claude/projects/-p/c1.jsonl", [
            # A message is a line per content block, each carrying the whole message's usage.
            first, first,
            _claude_turn("2", "claude-opus-5-5", input_tokens=100, output_tokens=700),
        ])
        self.codex_log = _jsonl(
            self.run / "sessions/codex/sessions/2026/10/02/rollout-2026-10-02T00-00-07-x9.jsonl", [
                {"type": "turn_context", "payload": {"model": "gpt-6-astra", "cwd": "/p"}},
                # Running totals, the last of which is the session's; input includes the cached part.
                _codex_count(input_tokens=1_000, cached_input_tokens=600, output_tokens=50),
                _codex_count(input_tokens=3_000, cached_input_tokens=2_000, output_tokens=200),
            ])
        self.began = hmz.transcripts._parse_ts("2026-10-02T00:00:00.000Z")

    def _spending(self, events=None, now=None):
        return hmz._spending(self.epic, events if events is not None else hmz._events(self.epic),
                             self.home, now=self.began + 125 if now is None else now)

    def test_tokens_summed_once_per_message_and_priced_per_model(self):
        s = self._spending()
        self.assertEqual(s["tokens"], {"input": 1_102, "output": 1_200,
                                       "cache_read": 12_000, "cache_write": 5_000})
        # Opus: 102×4 + 1000×20 + 10000×0.2 + 5000×5; Astra: 1000×10 + 2000×1 + 200×50 — per million.
        self.assertAlmostEqual(s["cost"], 0.047408 + 0.022)
        self.assertFalse(s["cost_floor"])
        self.assertEqual(s["spend_label"], "$0.07 · 1.2k out · 2m 5s")
        self.assertEqual(s["budget_label"], "15h, $150")
        self.assertFalse(s["over_budget"])
        self.assertEqual(s["spend_title"],
                         "input 1.1k · output 1.2k · cache_read 12.0k · cache_write 5.0k"
                         " — claude-opus-5-5, gpt-6-astra")

    def test_a_model_the_list_lacks_makes_the_bill_a_floor(self):
        prices = {"models": {"claude-opus-5-5": PRICES["models"]["claude-opus-5-5"]}}
        (self.home / "prices.json").write_text(json.dumps(prices))
        os.utime(self.home / "prices.json", (2_000, 2_000))  # a changed list is re-read
        s = self._spending()
        self.assertAlmostEqual(s["cost"], 0.047408)
        self.assertTrue(s["cost_floor"])
        self.assertTrue(s["spend_label"].startswith("$0.05+ · "))
        self.assertIn("counted, not billed", s["spend_title"])

    def test_no_price_list_counts_tokens_only(self):
        (self.home / "prices.json").unlink()
        s = self._spending()
        self.assertIsNone(s["cost"])
        self.assertEqual(s["spend_label"], "1.2k out · 2m 5s")

    def test_hmzs_own_total_once_the_run_ended(self):
        events = hmz._events(self.epic) + [
            {"event": "usage", "at": "2026-10-02T00:01:59.300Z", "cost": 0.8187342,
             "output_tokens": 12697, "seconds": 119.227725},
            {"event": "ended", "at": "2026-10-02T00:01:59.301Z", "how": "done"}]
        s = self._spending(events)
        self.assertAlmostEqual(s["cost"], 0.8187342)
        self.assertEqual(s["output_tokens"], 12697)
        self.assertEqual(s["spend_label"], "$0.82 · 12.7k out · 1m 59s")
        self.assertTrue(s["spend_title"].startswith("hmz's own total"))

    def test_a_run_from_before_usage_lines_ends_on_its_ended_line(self):
        events = hmz._events(self.epic) + [
            {"event": "ended", "at": "2026-10-02T00:03:00.000Z", "how": "stopped"}]
        self.assertEqual(self._spending(events)["elapsed_s"], 180.0)

    def test_log_is_read_on_from_where_the_last_poll_stopped(self):
        before = self._spending()["tokens"]["output"]
        with self.claude_log.open("a") as f:
            f.write(json.dumps(_claude_turn("3", "claude-opus-5-5", output_tokens=5)) + "\n")
            f.write('{"type": "assistant", "message": {"id": "msg_4", "usage": {"output_tok')  # mid-write
        self.assertEqual(self._spending()["tokens"]["output"], before + 5)
        # Rewritten shorter — a fresh session under the old name — it is read over.
        _jsonl(self.claude_log, [_claude_turn("9", "claude-opus-5-5", output_tokens=1)])
        self.assertEqual(self._spending()["tokens"]["output"], 201)

    def test_a_sub_agents_tokens_are_the_runs(self):
        # A sub-agent Claude starts logs under its session; the timeline leaves
        # it out, the bill takes it in, as hmz's own does.
        _jsonl(self.run / "sessions/claude/projects/-p/c1/subagents/agent-7.jsonl", [
            {**_claude_turn("7", "claude-opus-5-5", output_tokens=40), "isSidechain": True}])
        self.assertEqual(self._spending()["tokens"]["output"], 1_240)
        self.assertEqual(len(hmz._logs(self.epic, hmz._opened(self.epic)[0])), 1)
        self.assertNotIn("40", [e["text"] for e in hmz.hmz_timeline(self.epic)])

    def test_over_budget(self):
        events = hmz._events(self.epic)
        events[0]["budget"] = {"duration": "PT2M", "cost": None, "output_tokens": None, "graceful": False}
        s = self._spending(events)
        self.assertTrue(s["over_budget"])
        self.assertEqual(s["budget_label"], "2m, even mid-turn")

    def test_budget_as_hmz_writes_it(self):
        chat = hmz._budget({"budget": {"duration": None, "cost": "Infinity",
                                       "output_tokens": None, "graceful": True}})
        self.assertEqual(chat, {"cost": None, "duration_s": None, "output_tokens": None,
                                "graceful": True})
        self.assertEqual(hmz._budget_label(chat), "no limit")
        self.assertEqual(hmz._budget_label(hmz._budget({"budget": {
            "duration": "P1DT2H30M", "cost": 5, "output_tokens": 20_000, "graceful": False}})),
            "1d 2h, 20.0k out, $5.00, even mid-turn")
        self.assertIsNone(hmz._budget({}))
        self.assertEqual(hmz._budget_label(None), "")

    def test_durations(self):
        self.assertEqual(hmz._seconds("PT15H"), 54_000)
        self.assertEqual(hmz._seconds("PT0.5S"), 0.5)
        self.assertEqual(hmz._seconds(90), 90.0)
        self.assertIsNone(hmz._seconds(None))
        self.assertIsNone(hmz._seconds("PT"))
        self.assertEqual([hmz._clock(s) for s in (45, 125, 300, 4_320, 54_000, 95_400)],
                         ["45s", "2m 5s", "5m", "1h 12m", "15h", "1d 2h"])

    def test_prices_match_as_hmz_matches(self):
        prices = hmz._prices(self.home)
        opus = PRICES["models"]["claude-opus-5-5"]["per_million"]
        for spelled in ("claude-opus-5-5", "anthropic/claude-opus-5-5", "Claude Opus 5.5",
                        "us.anthropic.claude-opus-5-5-v1:0", "claude-opus-5-5-20260301",
                        "bedrock-claude-opus-5-5"):
            self.assertEqual(hmz._price(spelled, prices), opus, spelled)
        self.assertIsNone(hmz._price("claude-opus-5", prices))  # a near miss is a miss
        self.assertIsNone(hmz._price("<synthetic>", prices))

    def test_money_and_counts_as_hmz_writes_them(self):
        self.assertEqual([hmz._money(d) for d in (0, 0.0012, 0.47, 12.5, 150)],
                         ["$0.00", "$0.0012", "$0.47", "$12.50", "$150"])
        self.assertEqual([hmz._thousands(n) for n in (8, 12_697, 2_400_000)],
                         ["8", "12.7k", "2.40M"])


if __name__ == "__main__":
    unittest.main()

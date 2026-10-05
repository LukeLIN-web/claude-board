"""Tests for humanize (hmz) cards: telling the interface from hmz's headless
processes, finding the newest run of a directory, reading a run's epic, and
billing the sessions it keeps.
"""
import json
import os
import unittest
from unittest import mock

from core import hmz
from tests.helpers import make_window, scratch_dir, write_jsonl

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


def _window(**over):
    """An hmz card running in /home/u/proj, with no run yet unless given one."""
    return make_window(**{"pid": 7, "session_id": "hmz-7", "cwd": "/home/u/proj",
                          "project_slug": "-home-u-proj", "tty": "/dev/pts/1",
                          "platform": "hmz", **over})


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
        self.base = scratch_dir()
        home = mock.patch.object(hmz, "HMZ_HOME", self.base)
        home.start()
        self.addCleanup(home.stop)

    def _run(self, cwd_slug, name, events):
        return write_jsonl(events, self.base / "epics" / cwd_slug / name / "epic.jsonl")

    def test_newest_run_of_the_directory(self):
        self._run("-home-u-tmp-hmz-aot", "20261001T090000.000Z-aaaaaa", ENDED)
        newest = self._run("-home-u-tmp-hmz-aot", "20261002T005030.513Z-a779c6", RUN)
        self._run("-home-u-other", "20261003T000000.000Z-bbbbbb", RUN)
        self.assertEqual(hmz._latest_epic("/home/u/tmp/hmz-aot"), newest)

    def test_dir_without_runs(self):
        self.assertIsNone(hmz._latest_epic("/home/u/never.ran"))

    def test_home_before_and_after_the_rename(self):
        # hmz moved ~/.humanize to ~/.hmz; one from before the move keeps the old name.
        base = self.base
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
        other = self.base / "elsewhere"
        epic = write_jsonl(RUN[:1], other / "epics" / "-home-u-x"
                           / "20261002T000000.000Z-cccccc" / "epic.jsonl")
        self.assertIsNone(hmz._latest_epic("/home/u/x"))
        self.assertEqual(hmz._latest_epic("/home/u/x", other), epic)


class _HmzHomeTest(unittest.TestCase):
    """hmz's home (where its history.jsonl lives) is a fresh, empty directory."""

    def setUp(self):
        self.home = scratch_dir()
        home = mock.patch.object(hmz, "_home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)


class TestPromptTaken(_HmzHomeTest):
    """A send to hmz succeeds on hmz's own record of the line, never on an
    emptied composer."""

    CWD = "/home/u/proj"

    def setUp(self):
        super().setUp()
        self.history = self.home / "history.jsonl"

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
        shown.assert_called_once_with("%1", "again", "❯")

    def test_a_repeated_clear_is_taken_once_the_composer_lets_go(self):
        # …which a /clear wipes with the rest of the screen. The live miss: a
        # second Clear on the card said "the composer emptied but the prompt
        # was never taken".
        self._say("/clear")
        taken = hmz.prompt_taken(1, self.CWD, "/clear", "%1")
        with mock.patch.object(hmz.tmux, "_composer_has_tail", return_value=True) as held:
            self.assertFalse(taken())
        held.assert_called_once_with("%1", "/clear", "❯")
        with mock.patch.object(hmz.tmux, "_composer_has_tail", return_value=False):
            self.assertTrue(taken())


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
        w = _window(status=status)
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
        w = _window()
        with mock.patch.object(hmz, "_discover", return_value=[(w, [])]), \
                mock.patch.object(hmz.tmux, "pane_for_tty", return_value="%1"), \
                mock.patch.object(hmz.tmux, "capture_pane",
                                  return_value={"ok": True, "text": self.SETUP}):
            d = hmz.hmz_window_dicts()[0]
        self.assertEqual(d["triage"], "stalled")  # not waiting_perm: no Quick Approve "1"
        self.assertEqual(d["triage_reason"], "停在菜单：parallel_flame_chase")

    def test_the_timeline_says_so(self):
        import app
        w = _window()
        with mock.patch.object(app.sessions, "find_window", return_value=w), \
                mock.patch.object(hmz, "_home", return_value=scratch_dir() / "no-hmz"), \
                mock.patch.object(hmz.tmux, "pane_for_tty", return_value="%1"), \
                mock.patch.object(hmz.tmux, "capture_pane",
                                  return_value={"ok": True, "text": self.SETUP}):
            r = app.api_timeline("7")
        self.assertEqual(r["note"], hmz.MENU_NOTE.format("parallel_flame_chase"))


class TestTyped(_HmzHomeTest):
    """The lines typed into one hmz, read off the history it shares with every
    other hmz of its home."""

    CWD = "/home/u/robot"

    def setUp(self):
        super().setUp()
        write_jsonl(path=self.home / "history.jsonl", rows=[
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
        epic = write_jsonl([
            {"event": "began", "at": "2026-10-05T00:35:00Z", "flow": "rlar", "task": "fix  it"},
        ], self.home / "run" / "epic.jsonl")
        typed = [{"at": "2026-10-05T00:34:42Z", "workdir": self.CWD, "text": "$nosuch x"},
                 {"at": "2026-10-05T00:34:59Z", "workdir": self.CWD, "text": "$rlar fix it"}]
        self.assertEqual([e["text"] for e in hmz.hmz_timeline(epic, typed=typed)],
                         ["$nosuch x", "$rlar fix  it"])


class TestNoRunNote(_HmzHomeTest):
    def setUp(self):
        super().setUp()
        # The timeline looks for a setup menu on the card's pane; there is none.
        pane = mock.patch.object(hmz.tmux, "pane_for_tty", return_value=None)
        pane.start()
        self.addCleanup(pane.stop)

    def test_timeline_says_why_it_is_empty(self):
        import app
        with mock.patch.object(app.sessions, "find_window", return_value=_window()):
            r = app.api_timeline("7")
        self.assertEqual(r["events"], [])
        self.assertEqual(r["note"], hmz.NO_RUN_NOTE)

    def test_typed_into_but_no_run(self):
        # The live miss: hmz took `$parallel_flame_chase …`, had no such flow,
        # and the card said nothing had been typed.
        import app
        write_jsonl([{"at": "2026-10-05T00:34:42Z", "workdir": "/home/u/proj", "text": "$nosuch x"}],
                    self.home / "history.jsonl")
        with mock.patch.object(app.sessions, "find_window", return_value=_window()):
            r = app.api_timeline("7")
        self.assertEqual([e["text"] for e in r["events"]], ["$nosuch x"])
        self.assertEqual(r["note"], hmz.TYPED_NO_RUN_NOTE)

    def test_no_note_once_a_run_exists(self):
        import app
        epic = str(write_jsonl(RUN))
        with mock.patch.object(app.sessions, "find_window", return_value=_window(transcript_path=epic)):
            r = app.api_timeline("7")
        self.assertIsNone(r["note"])
        self.assertEqual(r["events"][0]["kind"], "user_text")


class TestClear(_HmzHomeTest):
    """hmz's /clear clears its screen and leaves every run where it was, so the
    card drops what came before the newest one itself."""

    CWD = "/home/u/proj"
    # When the live miss's Clear was written down, to the microsecond.
    CLEARED = "2026-10-05T15:29:44.001872Z"

    def setUp(self):
        super().setUp()
        pane = mock.patch.object(hmz.tmux, "pane_for_tty", return_value=None)
        pane.start()
        self.addCleanup(pane.stop)
        stamps = mock.patch.dict(hmz.codex._cleared_at_ms, clear=True)
        stamps.start()
        self.addCleanup(stamps.stop)

    def _say(self, text, at=CLEARED):
        with (self.home / "history.jsonl").open("a") as f:
            f.write(json.dumps({"at": at, "workdir": self.CWD, "text": text}) + "\n")

    @staticmethod
    def _ms(at):
        return hmz.transcripts._parse_ts(at) * 1000

    def test_the_newest_clear_it_wrote_down(self):
        typed = [{"at": "2026-10-02T01:00:00Z", "text": "/clear"},
                 {"at": "2026-10-02T01:05:00Z", "text": "/clear now"},  # run all the same
                 {"at": "2026-10-02T01:06:00Z", "text": " /clear"},  # said to the flow
                 {"at": "2026-10-02T01:07:00Z", "text": "/clears"}]  # no such command
        self.assertEqual(hmz.cleared_at_ms(typed), self._ms("2026-10-02T01:05:00Z"))
        self.assertEqual(hmz.cleared_at_ms([]), 0)

    def test_the_boards_stamp_when_later(self):
        # hmz doesn't write down a /clear that repeats its last line.
        typed = [{"at": "2026-10-02T01:00:00Z", "text": "/clear"}]
        later = int(self._ms("2026-10-02T02:00:00Z"))
        self.assertEqual(hmz.cleared_at_ms(typed, later), later)

    def test_the_timeline_starts_after_it(self):
        typed = [{"at": "2026-10-02T00:51:00Z", "workdir": self.CWD, "text": "/clear"},
                 {"at": "2026-10-02T00:55:00Z", "workdir": self.CWD, "text": "$nosuch x"}]
        ev = hmz.hmz_timeline(write_jsonl(ENDED), typed=typed,
                              since_ms=hmz.cleared_at_ms(typed))
        self.assertEqual([e["text"] for e in ev],
                         ["worker opened a claude session", "$nosuch x", "run ended: done"])

    def test_a_cleared_card_says_why_it_is_empty(self):
        # The live miss: Clear on a card whose run had stopped hours before
        # cleared hmz's screen, and the card went on showing the run.
        import app
        self._say("/clear")
        w = _window(transcript_path=str(write_jsonl(ENDED)))
        with mock.patch.object(app.sessions, "find_window", return_value=w):
            r = app.api_timeline("7")
        self.assertEqual(r["events"], [])
        self.assertEqual(r["note"], hmz.CLEARED_NOTE)

    def test_a_clear_hmz_did_not_write_down(self):
        import app
        hmz.codex.mark_cleared(7)
        w = _window(transcript_path=str(write_jsonl(ENDED)))
        with mock.patch.object(app.sessions, "find_window", return_value=w):
            r = app.api_timeline("7")
        self.assertEqual(r["note"], hmz.CLEARED_NOTE)

    def test_the_card_forgets_a_run_over_by_then(self):
        failed = ENDED[:-1] + [{**ENDED[-1], "how": "failed"}]
        w = _window(transcript_path=str(write_jsonl(failed)))
        self._say("/clear")
        with mock.patch.object(hmz, "_discover", return_value=[(w, failed)]):
            d = hmz.hmz_window_dicts()[0]
        self.assertEqual((d["first_input"], d["current_task"], d["last_error"]), ("", None, None))
        self.assertEqual(d["model"], "claude/claude-opus-5-5")  # as hmz's status bar keeps it

    def test_a_run_still_going_stays(self):
        w = _window(transcript_path=str(write_jsonl(RUN)), status="busy")
        self._say("/clear")
        with mock.patch.object(hmz, "_discover", return_value=[(w, RUN)]):
            d = hmz.hmz_window_dicts()[0]
        self.assertEqual(d["first_input"], "split todo.md across workers")
        self.assertEqual(d["current_task"], "commander_delegate · worker")


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
        ev = hmz.hmz_timeline(str(write_jsonl(ENDED)))
        self.assertEqual([e["kind"] for e in ev],
                         ["user_text", "assistant_text", "assistant_text", "assistant_text"])
        self.assertTrue(ev[0]["text"].startswith("$commander_delegate split todo.md"))
        self.assertEqual(ev[-1]["text"], "run ended: done")


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
        self.home = scratch_dir()
        (self.home / "prices.json").write_text(json.dumps(PRICES))
        self.run = self.home / "epics" / "-p" / "20261002T000000.000Z-a1b2c3"
        self.epic = write_jsonl(path=self.run / "epic.jsonl", rows=[
            {"event": "began", "at": "2026-10-02T00:00:00.000Z", "flow": "rlar", "task": "fix",
             "budget": {"duration": "PT15H", "cost": 150.0, "output_tokens": None, "graceful": True}},
            {"event": "opened", "at": "2026-10-02T00:00:05.000Z", "agent": "writer",
             "backend": "claude", "session": "c1", "where": "sessions/claude"},
            {"event": "opened", "at": "2026-10-02T00:00:08.000Z", "agent": "reviewer",
             "backend": "codex", "session": "x9", "where": "sessions/codex"},
        ])
        first = _claude_turn("1", "claude-opus-5-5", input_tokens=2, output_tokens=300,
                             cache_read_input_tokens=10_000, cache_creation_input_tokens=5_000)
        self.claude_log = write_jsonl(path=self.run / "sessions/claude/projects/-p/c1.jsonl", rows=[
            # A message is a line per content block, each carrying the whole message's usage.
            first, first,
            _claude_turn("2", "claude-opus-5-5", input_tokens=100, output_tokens=700),
        ])
        self.codex_log = write_jsonl(path=self.run / "sessions/codex/sessions/2026/10/02"
                                     / "rollout-2026-10-02T00-00-07-x9.jsonl", rows=[
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
        self.assertEqual(s["spend_label"], "$0.07 · 1.2k out · " + hmz.ELAPSED)
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
        self.assertEqual(s["spend_label"], "1.2k out · " + hmz.ELAPSED)

    def test_a_running_runs_clock_is_left_to_the_page(self):
        # It moves every second; written into the label, it would have the board
        # re-send its snapshot on every tick for as long as the run goes. The
        # label marks where the clock goes and elapsed_s says what it read.
        early, late = self._spending(now=self.began + 125), self._spending(now=self.began + 4_325)
        self.assertEqual(early["spend_label"], late["spend_label"])
        self.assertTrue(early["spend_label"].endswith(" · " + hmz.ELAPSED))
        self.assertEqual((early["elapsed_s"], late["elapsed_s"]), (125, 4_325))

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
        write_jsonl([_claude_turn("9", "claude-opus-5-5", output_tokens=1)], self.claude_log)
        self.assertEqual(self._spending()["tokens"]["output"], 201)

    def test_a_sub_agents_tokens_are_the_runs(self):
        # A sub-agent Claude starts logs under its session; hmz bills it as the run's.
        write_jsonl([{**_claude_turn("7", "claude-opus-5-5", output_tokens=40), "isSidechain": True}],
                    self.run / "sessions/claude/projects/-p/c1/subagents/agent-7.jsonl")
        self.assertEqual(self._spending()["tokens"]["output"], 1_240)

    def test_over_budget(self):
        events = list(hmz._events(self.epic))  # the read is shared: change a copy
        events[0] = {**events[0], "budget": {"duration": "PT2M", "cost": None,
                                             "output_tokens": None, "graceful": False}}
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


class TestRunOfCalledFlows(unittest.TestCase):
    """A run that does its work in flows it calls: the sessions are opened inside
    them, written down in their records — a lane's only once its first turn has
    landed — and logged under the run's sessions/. The card's course of the run
    is the epic's own lines; the bill reads every log under sessions/."""

    def setUp(self):
        self.home = scratch_dir()
        (self.home / "prices.json").write_text(json.dumps(PRICES))
        self.run = self.home / "epics" / "-p" / "20261005T013920.331Z-3196f3"
        self.epic = write_jsonl(path=self.run / "epic.jsonl", rows=[
            {"event": "began", "at": "2026-10-05T01:39:20.331Z", "flow": "parallel_flame_chase",
             "task": "lift", "budget": {"duration": "PT15H", "cost": 150.0,
                                        "output_tokens": None, "graceful": True}},
            {"event": "called", "at": "2026-10-05T01:39:22.428Z", "flow": "parallel_flame_chase:plan",
             "epic": "epic.parallel_flame_chase-plan_1c830f.jsonl"},
            {"event": "called", "at": "2026-10-05T01:41:03.628Z",
             "flow": "parallel_flame_chase:lane_turn",
             "epic": "epic.parallel_flame_chase-lane_turn_5dbd4d.jsonl"},
        ])
        # The plan's session: its one turn landed, so it is written down.
        write_jsonl(path=self.run / "epic.parallel_flame_chase-plan_1c830f.jsonl", rows=[
            {"event": "began", "at": "2026-10-05T01:39:22.429Z", "flow": "parallel_flame_chase:plan"},
            {"event": "opened", "at": "2026-10-05T01:41:03.167Z", "agent": "coordinator",
             "backend": "claude", "session": "0056c5ba-0692-40cf-98e2-bb016be18722",
             "where": "sessions/claude"},
            {"event": "ended", "at": "2026-10-05T01:41:03.169Z", "how": "done"},
        ])
        # The lane's: an hour into its first turn, nothing but `began` on its record.
        write_jsonl(path=self.run / "epic.parallel_flame_chase-lane_turn_5dbd4d.jsonl", rows=[
            {"event": "began", "at": "2026-10-05T01:41:03.628Z",
             "flow": "parallel_flame_chase:lane_turn"},
        ])
        projects = self.run / "sessions" / "claude" / "projects"
        write_jsonl(path=projects / "-planning" / "0056c5ba-0692-40cf-98e2-bb016be18722.jsonl", rows=[
            {"type": "user", "timestamp": "2026-10-05T01:39:23.603Z",
             "message": {"role": "user", "content": "plan it"}},
            _claude_turn("p", "claude-opus-5-5", input_tokens=12, output_tokens=1_000),
        ])
        self.lane_log = write_jsonl(path=projects / "-lane-2"
                                    / "3eb6cb8d-3c12-499f-b67c-54515abd8495.jsonl", rows=[
            # Claude's first lines are its queue's, stamped before the turn itself.
            {"type": "queue-operation", "operation": "enqueue",
             "timestamp": "2026-10-05T01:41:04.766Z", "sessionId": "3eb6cb8d"},
            {"type": "user", "timestamp": "2026-10-05T01:41:04.797Z",
             "message": {"role": "user", "content": "You are lane-2-actor-a"}},
            _claude_turn("l1", "claude-opus-5-5", input_tokens=100, output_tokens=40_000,
                         cache_read_input_tokens=5_000_000),
        ])
        self.began = hmz.transcripts._parse_ts("2026-10-05T01:39:20.331Z")

    def _spending(self):
        return hmz._spending(self.epic, hmz._events(self.epic), self.home, now=self.began + 2753)

    def test_a_session_in_its_first_turn_is_on_the_bill(self):
        s = self._spending()
        # plan: 12×4 + 1000×20; lane: 100×4 + 40000×20 + 5M×0.2 — per million.
        self.assertAlmostEqual(s["cost"], 0.020048 + 1.8004)
        self.assertEqual(s["output_tokens"], 41_000)
        self.assertEqual(s["spend_label"], "$1.82 · 41.0k out · " + hmz.ELAPSED)
        self.assertEqual(s["elapsed_s"], 2753)

    def test_its_subagents_are_billed_too(self):
        write_jsonl([_claude_turn("sub", "claude-opus-5-5", output_tokens=1_000)],
                    self.lane_log.parent / "3eb6cb8d-3c12-499f-b67c-54515abd8495" / "subagents"
                    / "agent-a1.jsonl")
        self.assertEqual(self._spending()["output_tokens"], 42_000)

    def test_a_codex_session_is_billed_off_its_rollout(self):
        write_jsonl(path=self.run / "sessions" / "codex" / "sessions" / "2026" / "10" / "05"
                    / "rollout-2026-10-05T01-41-05-01a0a14f-363a-76d2-a800-1f0dc14da2e0.jsonl", rows=[
            {"type": "turn_context", "payload": {"model": "gpt-6-astra", "cwd": "/p"}},
            _codex_count(input_tokens=1_000, cached_input_tokens=0, output_tokens=200),
        ])
        self.assertEqual(self._spending()["output_tokens"], 41_200)

    def test_an_ended_run_reads_no_log(self):
        with self.epic.open("a") as f:
            f.write(json.dumps({"event": "usage", "at": "2026-10-05T09:07:38.702Z", "cost": 151.27,
                                "output_tokens": 1_673_610, "seconds": 77_163.9}) + "\n")
            f.write(json.dumps({"event": "ended", "at": "2026-10-05T09:07:38.702Z",
                                "how": "stopped"}) + "\n")
        with mock.patch.object(hmz, "_spent_in", side_effect=AssertionError("read a log")):
            s = self._spending()
        self.assertEqual((s["cost"], s["output_tokens"]), (151.27, 1_673_610))
        self.assertTrue(s["over_budget"])  # $150 budget

    def test_the_task_is_the_flow_still_out(self):
        self.assertEqual(hmz._current_task(hmz._events(self.epic)), "parallel_flame_chase · "
                         "parallel_flame_chase:plan, parallel_flame_chase:lane_turn")
        # The epic grows as calls return and go out: it is read again.
        with self.epic.open("a") as f:
            f.write(json.dumps({"event": "returned", "at": "2026-10-05T01:41:03.169Z",
                                "flow": "parallel_flame_chase:plan",
                                "epic": "epic.parallel_flame_chase-plan_1c830f.jsonl"}) + "\n")
            f.write(json.dumps({"event": "called", "at": "2026-10-05T01:41:03.700Z",
                                "flow": "parallel_flame_chase:lane_turn",
                                "epic": "epic.parallel_flame_chase-lane_turn_8600ec.jsonl"}) + "\n")
        self.assertEqual(hmz._current_task(hmz._events(self.epic)),
                         "parallel_flame_chase · parallel_flame_chase:lane_turn ×2")

    def test_the_timeline_is_the_runs_own_lines(self):
        # Neither the sessions opened inside the called flows nor what they said.
        self.assertEqual([(e["extra"].get("agent"), e["text"]) for e in hmz.hmz_timeline(self.epic)],
                         [(None, "$parallel_flame_chase lift"),
                          (None, "called flow parallel_flame_chase:plan"),
                          (None, "called flow parallel_flame_chase:lane_turn")])

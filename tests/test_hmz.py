"""Tests for humanize (hmz) cards: telling the interface from hmz's headless
processes, finding the newest run of a directory, and reading a run's epic.
"""
import json
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


class TestNoRunNote(unittest.TestCase):
    def _window(self, transcript_path):
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


if __name__ == "__main__":
    unittest.main()

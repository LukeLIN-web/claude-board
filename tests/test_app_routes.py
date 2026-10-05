"""Tests for the two new tmux routes and the tmux_available snapshot flag.

httpx/TestClient is not a project dependency, so we exercise the route handler
functions directly (FastAPI's decorator returns the original function) and the
Pydantic request models at the model level.
"""
import contextlib
import os
import types
import unittest
from unittest import mock

import pydantic

import app as appmod
from tests.helpers import codex_rollout, scratch_dir, user_row, write_jsonl

# Everything _enriched_snapshot reads besides the cards themselves, each at a
# value that adds nothing to a card: no hmz runs, no shells, no permission
# toasts, no pane text (banner, dialog, queue, /btw aside), nothing in the
# transcript, no peers. Stubbed whole so no test here runs `ps` or `tmux` or
# reads a transcript path it made up. patrol.classify stays real: the triage it
# gives the card is what StaleDialogOpenTests is about.
_NEUTRAL = {
    "hmz_window_dicts": (appmod.hmz, []),
    "shell_descendant_counts": (appmod.sessions, {}),
    "pending_by_tty": (appmod.perms, {}),
    "available": (appmod.tmux, True),
    "codex_pane_model": (appmod.actions, ""),
    "pane_model": (appmod.actions, ""),
    "pane_dialog": (appmod.actions, None),
    "get_pane_queue": (appmod.actions, []),
    "current_task_hint": (appmod.transcripts, None),
    "current_model": (appmod.transcripts, ""),
    "extract_skills_used": (appmod.transcripts, []),
    "extract_memory_ops": (appmod.transcripts, []),
    "extract_background_tasks": (appmod.transcripts, []),
    "session_loop": (appmod.transcripts, None),
    "pending": (appmod.promptqueue, []),
    "maybe_capture": (appmod.btwcapture, None),
    "latest": (appmod.btwlog, None),
    "enabled": (appmod.peers, False),
}


@contextlib.contextmanager
def _snapshot(windows=(), codex=(), **overrides):
    """Run _enriched_snapshot over `windows` (Claude cards, as sessions.snapshot
    lists them) and `codex` (live Codex cards), with every other input at its
    _NEUTRAL value unless named in `overrides` (attribute name → return value).
    Yields the mocks by attribute name, for tests that assert on the calls."""
    unknown = set(overrides) - set(_NEUTRAL)
    if unknown:
        raise TypeError(f"_snapshot: nothing named {sorted(unknown)} is stubbed")
    with contextlib.ExitStack() as stack:
        stubs = {
            "snapshot": stack.enter_context(mock.patch.object(
                appmod.sessions, "snapshot",
                side_effect=lambda: {"windows": list(windows), "counts": {}, "ts": 0})),
            "codex_window_dicts": stack.enter_context(mock.patch.object(
                appmod.codex, "codex_window_dicts", side_effect=lambda: list(codex))),
        }
        for name, (module, value) in _NEUTRAL.items():
            stubs[name] = stack.enter_context(mock.patch.object(
                module, name, return_value=overrides.get(name, value)))
        yield types.SimpleNamespace(**stubs)


class RequestModelTests(unittest.TestCase):
    def test_create_body_requires_cwd(self):
        with self.assertRaises(pydantic.ValidationError):
            appmod.CreateBody()

    def test_prompt_body_requires_text(self):
        with self.assertRaises(pydantic.ValidationError):
            appmod.PromptBody()


class CreateRouteTests(unittest.TestCase):
    """Dispatch only. The route guards on the machine-local cwd filter first, so
    these say the cwd is visible rather than inheriting whether this machine's
    `CLAUDE_FLEET_CWD_INCLUDE` happens to cover /tmp — it doesn't, which is what
    used to fail them here and pass them on a box with no filter set. The guard
    itself is `test_hidden_cwd_is_refused` below."""

    def setUp(self):
        p = mock.patch.object(appmod.sessions, "_cwd_visible", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def test_dispatches_to_create_session(self):
        with mock.patch.object(appmod.actions, "create_session", return_value={"ok": True, "pane_id": "%1"}) as m:
            r = appmod.api_window_create(appmod.CreateBody(cwd="/tmp"))
        m.assert_called_once_with("/tmp", "claude")
        self.assertTrue(r["ok"])

    def test_spawn_dirs_keeps_only_existing_dirs(self):
        r = appmod.api_spawn_dirs(appmod.SpawnDirsBody(paths=["/tmp", "/no/such/dir", __file__]))
        self.assertEqual(r, {"ok": True, "paths": ["/tmp"]})

    def test_spawn_dirs_drops_hidden_dirs(self):
        # The spawn itself would be refused (test_hidden_cwd_is_refused), so the
        # picker must not offer it either.
        with mock.patch.object(appmod.sessions, "_cwd_visible", return_value=False):
            r = appmod.api_spawn_dirs(appmod.SpawnDirsBody(paths=["/tmp"]))
        self.assertEqual(r["paths"], [])

    def test_spawn_dirs_asks_the_peer_it_would_spawn_on(self):
        with mock.patch.object(appmod.peers, "configured", return_value={"63": "http://x"}), \
             mock.patch.object(appmod.peers, "forward", return_value={"ok": True, "paths": []}) as m:
            appmod.api_spawn_dirs(appmod.SpawnDirsBody(paths=["/tmp"], host="63"))
        m.assert_called_once_with("63", "POST", "/api/spawn-dirs", {"paths": ["/tmp"]})

    def test_dispatches_codex_platform(self):
        with mock.patch.object(appmod.actions, "create_session", return_value={"ok": True, "pane_id": "%1"}) as m:
            r = appmod.api_window_create(appmod.CreateBody(cwd="/tmp", platform="codex"))
        m.assert_called_once_with("/tmp", "codex")
        self.assertTrue(r["ok"])


class CreateRouteVisibilityTests(unittest.TestCase):
    def test_hidden_cwd_is_refused(self):
        # Spawning into a dir the board filters out would create a card that the
        # dashboard then refuses to show.
        import fastapi
        with mock.patch.object(appmod.sessions, "_cwd_visible", return_value=False), \
             mock.patch.object(appmod.actions, "create_session") as m:
            with self.assertRaises(fastapi.HTTPException) as cm:
                appmod.api_window_create(appmod.CreateBody(cwd="/tmp"))
        self.assertEqual(cm.exception.status_code, 403)
        m.assert_not_called()


class HistoryTimelineVisibilityTests(unittest.TestCase):
    """The archive timeline obeys the cwd allowlist on the transcript's own cwd:
    projects/-shared-ws-proj-evil reads, by its slug, as inside /shared/ws/proj."""

    def setUp(self):
        self.root = scratch_dir()
        write_jsonl([user_row("secret plans", "2026-09-08T20:40:00Z", cwd="/shared/ws/proj-evil")],
                    self.root / "-shared-ws-proj-evil" / "sid1.jsonl")
        env = mock.patch.dict("os.environ", {"CLAUDE_FLEET_CWD_INCLUDE": "/shared/ws/proj",
                                             "CLAUDE_FLEET_CWD_EXCLUDE": ""})
        env.start()
        appmod.sessions._reload_cwd_filters()
        self.addCleanup(lambda: (env.stop(), appmod.sessions._reload_cwd_filters()))
        p = mock.patch.object(appmod.sessions, "PROJECTS_DIR", self.root)
        p.start()
        self.addCleanup(p.stop)

    def test_a_sibling_outside_the_allowlist_is_not_served(self):
        import fastapi
        with self.assertRaises(fastapi.HTTPException) as cm:
            appmod.api_history_timeline("sid1")
        self.assertEqual(cm.exception.status_code, 404)


def _allow_only(test, include):
    """Run `test` under CLAUDE_FLEET_CWD_INCLUDE=`include` and no exclude list,
    with an empty ~/.claude/projects, so the archive timeline reaches past it."""
    env = mock.patch.dict("os.environ", {"CLAUDE_FLEET_CWD_INCLUDE": include,
                                         "CLAUDE_FLEET_CWD_EXCLUDE": ""})
    env.start()
    appmod.sessions._reload_cwd_filters()
    test.addCleanup(lambda: (env.stop(), appmod.sessions._reload_cwd_filters()))
    p = mock.patch.object(appmod.sessions, "PROJECTS_DIR", scratch_dir())
    p.start()
    test.addCleanup(p.stop)


def _timeline_404(test, session_id):
    import fastapi
    with test.assertRaises(fastapi.HTTPException) as cm:
        appmod.api_history_timeline(session_id)
    test.assertEqual(cm.exception.status_code, 404)


class CodexHistoryTimelineTests(unittest.TestCase):
    """The archive timeline serves a Codex rollout by its whole id only, and
    only while the cwd it records passes the filter."""

    IN = "019a0001-0000-7000-8000-00000000aaaa"
    EVIL = "019a0002-0000-7000-8000-00000000bbbb"

    def setUp(self):
        _allow_only(self, "/shared/ws/proj")
        self.root = scratch_dir()
        p = mock.patch.object(appmod.codex, "CODEX_SESSIONS_DIR", self.root)
        p.start()
        self.addCleanup(p.stop)
        self.inside = codex_rollout(self.root, self.IN, "/shared/ws/proj/x",
                                    [self._prompt("first page")])
        self.evil = codex_rollout(self.root, self.EVIL, "/shared/ws/proj-evil",
                                  [self._prompt("secret plans")])

    @staticmethod
    def _prompt(text):
        return {"timestamp": "2026-10-05T08:00:01Z", "type": "event_msg",
                "payload": {"type": "user_message", "message": text}}

    def _prompts(self, session_id):
        r = appmod.api_history_timeline(session_id)
        self.assertEqual(r["platform"], "codex")
        return [e["text"] for e in r["events"] if e["kind"] == "user_text"]

    def test_a_rollout_outside_the_allowlist_is_not_served(self):
        _timeline_404(self, self.EVIL)
        _timeline_404(self, self.evil.stem)

    def test_an_id_is_matched_whole(self):
        # Any piece of a rollout's name used to find one: here, the hidden one.
        for piece in ("2026", "019a", "rollout", self.EVIL[:8]):
            _timeline_404(self, piece)

    def test_found_by_its_session_id_or_its_file_stem(self):
        # A history row names the id, a search hit the stem.
        self.assertEqual(self._prompts(self.IN), ["first page"])
        self.assertEqual(self._prompts(self.inside.stem), ["first page"])

    def test_served_on_a_host_with_no_claude_projects(self):
        # A host that runs only Codex has no ~/.claude/projects to look in first.
        with mock.patch.object(appmod.sessions, "PROJECTS_DIR", self.root / "no-such-dir"):
            self.assertEqual(self._prompts(self.IN), ["first page"])

    def test_a_paginated_thread_opens_on_its_newest_page(self):
        page = codex_rollout(self.root, self.IN, "/shared/ws/proj/x",
                             [self._prompt("second page")],
                             page="019a0009-0000-7000-8000-00000000cccc",
                             time="2026-10-06T09-00-00")
        os.utime(self.inside, (1000, 1000))
        os.utime(page, (2000, 2000))
        self.assertEqual(self._prompts(self.IN), ["second page"])
        self.assertEqual(self._prompts(self.inside.stem), ["first page"])


class OpenCodeHistoryTimelineTests(unittest.TestCase):
    """The archive timeline serves an OpenCode session only while the directory
    it records passes the filter."""

    def setUp(self):
        import sqlite3
        _allow_only(self, "/shared/ws/proj")
        db = scratch_dir() / "opencode.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE session (id TEXT, title TEXT, directory TEXT,
                                  time_created INTEGER, time_updated INTEGER);
            CREATE TABLE message (id TEXT, session_id TEXT, data TEXT, time_created INTEGER);
            CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, data TEXT,
                               time_created INTEGER);
        """)
        for sid, directory in (("ses_in", "/shared/ws/proj/x"),
                               ("ses_evil", "/shared/ws/proj-evil")):
            conn.execute("INSERT INTO session VALUES (?, '', ?, 1, 1)", (sid, directory))
            conn.execute("INSERT INTO message VALUES (?, ?, ?, 1)",
                         (f"m_{sid}", sid, '{"role": "user"}'))
            conn.execute("INSERT INTO part VALUES (?, ?, ?, ?, 1)",
                         (f"p_{sid}", f"m_{sid}", sid, '{"type": "text", "text": "the plans"}'))
        conn.commit()
        conn.close()
        p = mock.patch("core.opencode.OPENCODE_DB", db)
        p.start()
        self.addCleanup(p.stop)

    def test_a_session_outside_the_allowlist_is_not_served(self):
        _timeline_404(self, "ses_evil")

    def test_a_session_inside_it_is(self):
        r = appmod.api_history_timeline("ses_in")
        self.assertEqual([e["text"] for e in r["events"]], ["the plans"])


class PromptRouteTests(unittest.TestCase):
    def test_dispatches_to_send_prompt(self):
        # The route now guards on a visible window before sending.
        with mock.patch.object(appmod.sessions, "find_window", return_value=object()), \
             mock.patch.object(appmod.actions, "send_prompt", return_value={"ok": True}) as m:
            r = appmod.api_window_prompt(4321, appmod.PromptBody(text="hi there"))
        m.assert_called_once_with(4321, "hi there")
        self.assertTrue(r["ok"])

    def test_prompt_blocked_for_hidden_window(self):
        import fastapi
        with mock.patch.object(appmod.sessions, "find_window", return_value=None), \
             mock.patch.object(appmod.actions, "send_prompt") as m:
            with self.assertRaises(fastapi.HTTPException):
                appmod.api_window_prompt(4321, appmod.PromptBody(text="hi there"))
        m.assert_not_called()


class BtwDismissRouteTests(unittest.TestCase):
    def test_dispatches_to_btwlog_dismiss(self):
        w = mock.Mock(session_id="sess1")
        with mock.patch.object(appmod.sessions, "find_window", return_value=w), \
             mock.patch.object(appmod.btwlog, "dismiss", return_value=True) as m:
            r = appmod.api_btw_dismiss(4321, appmod.BtwDismissBody(id=7))
        m.assert_called_once_with("sess1", 7)
        self.assertTrue(r["ok"])

    def test_error_without_session_id(self):
        w = mock.Mock(session_id=None)
        with mock.patch.object(appmod.sessions, "find_window", return_value=w), \
             mock.patch.object(appmod.btwlog, "dismiss") as m:
            r = appmod.api_btw_dismiss(4321, appmod.BtwDismissBody(id=7))
        m.assert_not_called()
        self.assertFalse(r["ok"])

    def test_blocked_for_hidden_window(self):
        import fastapi
        with mock.patch.object(appmod.sessions, "find_window", return_value=None), \
             mock.patch.object(appmod.btwlog, "dismiss") as m:
            with self.assertRaises(fastapi.HTTPException):
                appmod.api_btw_dismiss(4321, appmod.BtwDismissBody(id=7))
        m.assert_not_called()


class SnapshotFlagTests(unittest.TestCase):
    def test_tmux_available_present_with_zero_windows(self):
        with _snapshot(available=True):
            snap = appmod._enriched_snapshot()
        self.assertIn("tmux_available", snap)
        self.assertTrue(snap["tmux_available"])

    def test_tmux_available_reflects_false(self):
        with _snapshot(available=False):
            snap = appmod._enriched_snapshot()
        self.assertFalse(snap["tmux_available"])


class CardModelTests(unittest.TestCase):
    """The card carries the model the session is actually running on — without it,
    a switch driven from the board changes nothing visible."""

    def setUp(self):
        appmod._banner_models.clear()

    @staticmethod
    def _win(**over):
        # A live, visible session with a transcript; each test changes what it is about.
        return {"pid": 1, "status": "idle", "hidden": False, "alive": True,
                "tty": "pts/1", "transcript_path": "/t.jsonl", "cwd": "/x",
                "name": "s", "updated_at": 0, **over}

    def _run(self, win, model="claude-fable-5", banner=""):
        with _snapshot([win], current_model=model, pane_model=banner):
            return appmod._enriched_snapshot()["windows"][0]

    def test_card_reports_the_running_model(self):
        w = self._run(self._win())
        self.assertEqual(w["model"], "claude-fable-5")
        self.assertEqual(w["model_label"], "Fable 5")

    def test_no_transcript_means_no_model(self):
        w = self._run(self._win(transcript_path=None))
        self.assertEqual(w["model"], "")
        self.assertEqual(w["model_label"], "")

    def test_transcript_wins_over_the_banner(self):
        # The banner says what the session was on when it last started or was
        # cleared; the transcript says what actually answered since. When both
        # speak, the transcript is the one that can't be stale.
        w = self._run(self._win(), banner="Opus 5")
        self.assertEqual(w["model_label"], "Fable 5")
        self.assertEqual(w["model_source"], "transcript")

    def test_cleared_session_falls_back_to_its_banner(self):
        # /clear starts a fresh transcript: no assistant row, hence no model —
        # which is exactly when the card used to go blank.
        w = self._run(self._win(), model="", banner="Opus 5")
        self.assertEqual(w["model_label"], "Opus 5")
        self.assertEqual(w["model_source"], "banner")
        # No id is invented for it — the raw field stays empty.
        self.assertEqual(w["model"], "")

    def test_unreadable_banner_leaves_the_readout_blank(self):
        w = self._run(self._win(), model="", banner="")
        self.assertEqual(w["model_label"], "")
        self.assertEqual(w["model_source"], "")

    def test_banner_is_scraped_once_per_session_not_once_per_poll(self):
        # A banner is printed at startup and at /clear and never rewritten, so
        # one read answers for the whole session id — the poll runs every 2s and
        # must not spend a capture-pane on each card each time. Proven by the
        # cached answer surviving a pane that has since started saying something
        # else, which cannot happen for real within one session id.
        win = self._win(session_id="abc")
        self.assertEqual(self._run(win, model="", banner="Opus 5")["model_label"],
                         "Opus 5")
        self.assertEqual(self._run(win, model="", banner="Fable 5.1")["model_label"],
                         "Opus 5")

    def test_a_clear_re_reads_the_banner(self):
        # The new session id is the signal that the banner has been reprinted —
        # and that the model may have changed with it.
        first = self._run(self._win(session_id="abc"), model="", banner="Opus 5")
        second = self._run(self._win(session_id="def"), model="", banner="Fable 5.1")
        self.assertEqual(first["model_label"], "Opus 5")
        self.assertEqual(second["model_label"], "Fable 5.1")

    def test_dead_window_is_not_scraped(self):
        with _snapshot([self._win(alive=False, tty=None, transcript_path=None)]) as stubs:
            w = appmod._enriched_snapshot()["windows"][0]
        stubs.pane_model.assert_not_called()
        self.assertEqual(w["model_label"], "")

    def test_hidden_agent_does_not_borrow_its_parents_banner(self):
        # A `.slock` sub-session shares the parent's tty, so the banner in that
        # pane names the parent's model — and sub-agents routinely run on
        # another one.
        with _snapshot([self._win(status="unknown", hidden=True,
                                  cwd="/x/.slock/a")]) as stubs:
            w = appmod._enriched_snapshot()["windows"][0]
        stubs.pane_model.assert_not_called()
        self.assertEqual(w["model_label"], "")

    def test_cache_does_not_outlive_the_session(self):
        # A session that leaves the board takes its cached banner with it, so
        # the cache tracks the board rather than growing an entry per session
        # for the life of the process. Proven by the same pid and session id
        # coming back to a fresh read — which, while cached, it would not get
        # (test_banner_is_scraped_once_per_session_not_once_per_poll).
        win = self._win(session_id="abc")
        self.assertEqual(self._run(win, model="", banner="Opus 5")["model_label"],
                         "Opus 5")
        with _snapshot():
            appmod._enriched_snapshot()  # the session is gone from this poll
        self.assertEqual(self._run(win, model="", banner="Fable 5.1")["model_label"],
                         "Fable 5.1")


class HiddenAgentQueueTests(unittest.TestCase):
    """`.slock` agent sub-sessions never write a `status` field (it normalizes to
    "unknown"), yet their pid+tty still back the queue. The Queued list must
    render for them, not only for windows that report `status == "busy"`."""

    def _run(self, win):
        with _snapshot([win], pending=["/btw"]):
            return appmod._enriched_snapshot()

    def test_queue_renders_for_hidden_agent_without_busy_status(self):
        win = {"pid": 4163977, "status": "unknown", "hidden": True, "alive": True,
               "tty": "pts/14", "transcript_path": None, "name": "agent", "cwd": "/x",
               "updated_at": 0}
        out = self._run(win)
        self.assertEqual(out["windows"][0]["queued"],
                         [{"text": "/btw", "source": "dashboard"}])

    def test_dead_hidden_agent_has_no_queue(self):
        win = {"pid": 4163977, "status": "unknown", "hidden": True, "alive": False,
               "tty": None, "transcript_path": None, "name": "agent", "cwd": "/x",
               "updated_at": 0}
        out = self._run(win)
        self.assertEqual(out["windows"][0]["queued"], [])


class DiffSignatureTests(unittest.TestCase):
    """The SSE watcher only broadcasts when `diff_signature` changes. A queued
    prompt being consumed (or added) while status/updated_at stay the same must
    still change the signature, or the card keeps showing a stale queue."""

    def _win(self, queued):
        # `key` is the card's address (host-qualified pid); the signature is
        # built on it, since pids only identify a card within one host.
        return {"pid": 100, "key": "100", "status": "busy", "waiting_for": None,
                "updated_at": 5, "queued": queued}

    def test_queue_change_alone_changes_signature(self):
        st = appmod.State()
        before = {"windows": [self._win(
            [{"text": "/btw", "source": "dashboard"}])], "counts": {}, "ts": 0}
        after = {"windows": [self._win([])], "counts": {}, "ts": 0}
        self.assertNotEqual(
            st.diff_signature(before), st.diff_signature(after),
            "consuming a queued prompt must change the broadcast signature")

    def test_btw_change_alone_changes_signature(self):
        # Archiving or dismissing an aside on an otherwise idle session must
        # still broadcast, or the card keeps showing a stale (or dismissed) aside.
        st = appmod.State()
        w_with = self._win([])
        w_with["btw"] = {"id": 1, "ts": 0, "question": "q", "answer": "a"}
        w_without = self._win([])
        w_without["btw"] = None
        self.assertNotEqual(
            st.diff_signature({"windows": [w_with], "counts": {}, "ts": 0}),
            st.diff_signature({"windows": [w_without], "counts": {}, "ts": 0}),
            "dismissing the card's aside must change the broadcast signature")


class StaleDialogOpenTests(unittest.TestCase):
    """Claude writes status="waiting" / waitingFor="dialog open" for ANY open
    overlay — including the /goal panel, which has nothing to answer and does
    not block the agent. Such a window must not raise the red waiting card
    (Quick Approve would type "1" into the input box) nor count as waiting in
    the header; a verifiable picker in the pane keeps the normal behavior."""

    def _run(self, dialog):
        import time
        win = {"pid": 100, "status": "waiting", "waiting_for": "dialog open",
               "hidden": False, "alive": True, "tty": "/dev/pts/9",
               "transcript_path": None, "name": "w", "cwd": "/x",
               "updated_at": int(time.time() * 1000), "idle_seconds": 0}
        with _snapshot([win], pane_dialog=dialog):
            return appmod._enriched_snapshot()["windows"][0]

    def test_dialog_without_menu_is_not_waiting(self):
        w = self._run({"menu": False, "trust": False})
        self.assertNotEqual(w["triage"], "waiting_perm")
        self.assertNotEqual(w["status"], "waiting")

    def test_dialog_with_real_menu_stays_waiting(self):
        w = self._run({"menu": True, "trust": False})
        self.assertEqual(w["triage"], "waiting_perm")
        self.assertEqual(w["status"], "waiting")

    def test_unverifiable_pane_stays_waiting(self):
        # tmux can't see the pane: keep the conservative waiting card.
        w = self._run(None)
        self.assertEqual(w["triage"], "waiting_perm")
        self.assertEqual(w["status"], "waiting")

    def test_trust_prompt_is_named_on_the_card(self):
        # "dialog open" tells the user nothing, and the trust prompt is answered
        # by its own route — so the card has to say which dialog it is stuck on.
        w = self._run({"menu": True, "trust": True})
        self.assertEqual(w["triage"], "waiting_perm")
        self.assertEqual(w["waiting_for"], "trust prompt")
        self.assertEqual(w["triage_reason"], "trust prompt")


class CodexCardModelTests(unittest.TestCase):
    """A Codex card reads its model + effort off the pane's status line when it
    can, and off the rollout (what the last turn ran on) otherwise."""

    def _card(self):
        return {"pid": 7, "platform": "codex", "status": "idle", "hidden": False,
                "alive": True, "tty": "pts/7", "transcript_path": "/r.jsonl",
                "cwd": "/x", "name": None, "updated_at": 0, "triage": "",
                "model": "gpt-6-astra", "effort": "medium",
                "model_label": "gpt-6-astra medium", "model_source": "transcript"}

    def _run(self, pane_label):
        with _snapshot(codex=[self._card()], codex_pane_model=pane_label) as stubs:
            w = appmod._enriched_snapshot()["windows"][0]
        stubs.codex_pane_model.assert_called_once_with("pts/7")
        return w

    def test_status_line_wins(self):
        w = self._run("gpt-5.6-sol high")
        self.assertEqual(w["model_label"], "gpt-5.6-sol high")
        self.assertEqual(w["model_source"], "pane")

    def test_rollout_label_stands_without_a_readable_pane(self):
        w = self._run("")
        self.assertEqual(w["model_label"], "gpt-6-astra medium")
        self.assertEqual(w["model_source"], "transcript")


class ModelRouteTests(unittest.TestCase):
    def test_body_fields_are_optional(self):
        self.assertEqual(appmod.ModelBody(effort="high").model, "")
        self.assertEqual(appmod.ModelBody(model="opus").effort, "")

    def test_route_hands_both_fields_to_the_switch(self):
        with mock.patch.object(appmod, "_require_window"), \
             mock.patch.object(appmod.actions, "switch_model",
                               return_value={"ok": True}) as sw:
            appmod.api_window_model("1234", appmod.ModelBody(model="gpt-5.6-sol", effort="high"))
        sw.assert_called_once_with(1234, "gpt-5.6-sol", "high")


class HistoryLaunchRouteTests(unittest.TestCase):
    """Resume and fork from History open the session the same way, and both
    answer the resume picker the launch may stop on."""

    def _launch(self, route, opened=None):
        opened = opened or {"ok": True, "backend": "tmux", "pane_id": "%7"}
        with mock.patch.object(appmod, "_history_cwd", return_value="/tmp/proj"), \
             mock.patch.object(appmod.sessions, "find_window_by_session", return_value=None), \
             mock.patch.object(appmod.actions, "open_claude_window",
                               return_value=dict(opened)) as ocw, \
             mock.patch.object(appmod.actions, "confirm_resume_picker",
                               return_value={"confirmed": True, "waited": 0.4, "reason": ""}) as crp:
            r = route("sessX")
        return r, ocw, crp

    def test_fork_answers_the_resume_picker(self):
        r, ocw, crp = self._launch(appmod.api_history_fork)
        ocw.assert_called_once_with("/tmp/proj", ["--resume", "sessX", "--fork-session"])
        crp.assert_called_once_with("%7")
        self.assertEqual(r["action"], "forked")
        self.assertTrue(r["picker"]["confirmed"])

    def test_resume_answers_the_resume_picker(self):
        r, ocw, crp = self._launch(appmod.api_history_resume)
        ocw.assert_called_once_with("/tmp/proj", ["--resume", "sessX"])
        crp.assert_called_once_with("%7")
        self.assertEqual(r["action"], "resumed")

    def test_a_window_outside_tmux_has_no_pane_to_answer(self):
        r, _, crp = self._launch(appmod.api_history_fork, {"ok": True, "backend": "iterm"})
        crp.assert_not_called()
        self.assertNotIn("picker", r)

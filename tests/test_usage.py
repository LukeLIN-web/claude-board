"""Tests for core/usage.py: reading plan usage off Claude's /status and /usage,
and off codex app-server.

The panel fixtures are panes captured on v2.1.291: fixtures/usage_panel.txt once
the panel settled, fixtures/usage_panel_refreshing.txt the cached paint it shows
first (one limit short, "Refreshing…" below). The app-server replies are shaped
as codex-cli 0.160.0 answers them.
"""
import json
import os
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import app as appmod
from core import sessions, tmux, usage

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text()


class ParseLimitsTests(unittest.TestCase):
    def test_settled_panel_reads_every_limit(self):
        self.assertEqual(usage.parse_limits(_fixture("usage_panel.txt")), [
            {"label": "Current session", "used": 5,
             "resets": "1:19pm (America/Los_Angeles)"},
            {"label": "Current week (all models)", "used": 23,
             "resets": "Oct 11, 6:59pm (America/Los_Angeles)"},
            {"label": "Current week (Fable)", "used": 14,
             "resets": "Oct 11, 6:59pm (America/Los_Angeles)"},
        ])

    def test_contributor_lines_are_not_limits(self):
        # "83% of your usage was at >150k context" and the skill/subagent shares
        # are percentages too, of something else.
        labels = [lim["label"] for lim in usage.parse_limits(_fixture("usage_panel.txt"))]
        self.assertTrue(all(lab.startswith("Current") for lab in labels), labels)

    def test_an_empty_bar_still_has_its_heading(self):
        text = "   Current session\n" + " " * 50 + "0% used\n   Resets 3pm (UTC)\n"
        self.assertEqual(usage.parse_limits(text),
                         [{"label": "Current session", "used": 0, "resets": "3pm (UTC)"}])

    def test_heading_on_the_bar_line(self):
        text = "   Current session ██▌      5% used\n"
        self.assertEqual(usage.parse_limits(text),
                         [{"label": "Current session", "used": 5, "resets": ""}])


class SettledTests(unittest.TestCase):
    def test_settled_panel(self):
        self.assertTrue(usage.usage_settled(_fixture("usage_panel.txt")))

    def test_cached_paint_is_not_settled(self):
        text = _fixture("usage_panel_refreshing.txt")
        self.assertEqual(len(usage.parse_limits(text)), 2)
        self.assertFalse(usage.usage_settled(text))

    def test_composer_is_not_settled(self):
        self.assertFalse(usage.usage_settled("❯ \n  ? for shortcuts\n"))


class PanelSaysTests(unittest.TestCase):
    def test_says_what_follows_the_session_block(self):
        # Not a captured screen: what the tab shows for a login without plan
        # limits hasn't been seen. The session block and footer are real.
        panel = _fixture("usage_panel.txt")
        head = panel[:panel.index("   Current session")]
        text = head + "   Limits are shown for Claude subscriptions only\n\n   Esc to cancel\n"
        self.assertEqual(usage._panel_says(text),
                         "Limits are shown for Claude subscriptions only")

    def test_no_panel_says_nothing(self):
        self.assertEqual(usage._panel_says("❯ \n"), "")


class ParseAccountTests(unittest.TestCase):
    def test_status_tab_fields(self):
        text = ("   Settings  Status   Config   Usage   Stats\n\n"
                "   Version:           2.1.291\n"
                "   Login method:      Claude Max account\n"
                "   Organization:      Example Org\n"
                "   Email:             someone@example.com\n")
        self.assertEqual(usage.parse_account(text),
                         {"login": "Claude Max account", "email": "someone@example.com"})

    def test_missing_fields_are_empty(self):
        self.assertEqual(usage.parse_account("   Version:  2.1.291\n"),
                         {"login": "", "email": ""})


class OpenPanelTests(unittest.TestCase):
    """fixtures/status_panel_echo.txt is /status as one host drew it: short
    enough that the command's echo, "❯ /status", stays on screen above the
    panel, with no composer below it."""

    def _run(self, frames):
        """_open_panel("/status") over a pane that shows `frames` in turn (the
        last one from then on); the keys it pressed."""
        frames = list(frames)
        keys = []

        def capture(pane, scrollback=0):
            return {"ok": True, "text": frames.pop(0) if len(frames) > 1 else frames[0]}

        with mock.patch.object(tmux, "_send_until_landed", return_value=True), \
             mock.patch.object(tmux, "capture_pane", side_effect=capture), \
             mock.patch.object(tmux, "send_keys", side_effect=lambda p, *k: keys.extend(k)), \
             mock.patch.object(usage, "_SUBMIT_WAIT", 0.05), \
             mock.patch.object(tmux, "_SLASH_SETTLE", 0):
            opened = usage._open_panel("%0", "/status")
        return opened, keys

    def test_the_echo_reads_as_an_unsent_command_to_send_text(self):
        # Why _open_panel doesn't use send_text's submit check.
        self.assertTrue(tmux._tail_in(_fixture("status_panel_echo.txt"), "/status", "❯"))

    def test_an_open_panel_is_one_enter(self):
        opened, keys = self._run([_fixture("status_panel_echo.txt")])
        self.assertTrue(opened)
        self.assertEqual(keys, ["Enter"])

    def test_a_swallowed_enter_is_pressed_again(self):
        stranded = "─" * 40 + "\n❯ /status\n" + "─" * 40 + "\n"
        opened, keys = self._run([stranded] * 3 + [_fixture("status_panel_echo.txt")])
        self.assertTrue(opened)
        self.assertEqual(keys, ["Enter", "Enter"])

    def test_a_panel_slow_to_draw_gets_no_second_enter(self):
        submitted = "─" * 40 + "\n❯ \n" + "─" * 40 + "\n"
        opened, keys = self._run([submitted] * 3 + [_fixture("status_panel_echo.txt")])
        self.assertTrue(opened)
        self.assertEqual(keys, ["Enter"])


class ProbeHiddenTests(unittest.TestCase):
    def test_probe_dir_is_never_visible(self):
        self.assertFalse(sessions._cwd_visible(str(sessions.PROBE_CWD)))
        self.assertFalse(sessions._cwd_visible(str(sessions.PROBE_CWD / "sub")))

    def test_neighbours_stay_visible(self):
        self.assertTrue(sessions._cwd_visible(str(sessions.PROBE_CWD.parent)))
        self.assertTrue(sessions._cwd_visible(str(sessions.PROBE_CWD) + "-other"))


class PrivateServerTests(unittest.TestCase):
    def test_calls_in_the_block_go_to_the_private_server_and_it_is_killed(self):
        seen = []
        with mock.patch.object(tmux, "_run",
                               side_effect=lambda *a, **k: seen.append((a, tmux._socket_args()))):
            outside = tmux._socket_args()
            with tmux.private_server("probe-x"):
                inside = tmux._socket_args()
            after = tmux._socket_args()
        self.assertEqual(inside, ["-L", "probe-x"])
        self.assertEqual(after, outside)
        self.assertEqual(seen, [(("kill-server",), ["-L", "probe-x"])])

    def test_other_threads_keep_their_server(self):
        other = []
        with mock.patch.object(tmux, "_run"):
            outside = tmux._socket_args()
            with tmux.private_server("probe-y"):
                t = threading.Thread(target=lambda: other.append(tmux._socket_args()))
                t.start()
                t.join()
        self.assertEqual(other, [outside])


class ReadUsageTests(unittest.TestCase):
    def setUp(self):
        usage._last.clear()

    def tearDown(self):
        usage._last.clear()

    def test_concurrent_requests_share_one_probe(self):
        calls = []

        def slow_probe():
            calls.append(1)
            time.sleep(0.3)
            return {"ok": True, "limits": []}

        got = []
        with mock.patch.object(usage, "_probe", side_effect=slow_probe):
            first = threading.Thread(target=lambda: got.append(usage.read_usage()))
            first.start()
            time.sleep(0.1)  # the second asks while the first is probing
            second = threading.Thread(target=lambda: got.append(usage.read_usage()))
            second.start()
            first.join()
            second.join()
        self.assertEqual(len(calls), 1)
        self.assertIs(got[0], got[1])

    def test_a_later_request_probes_again(self):
        with mock.patch.object(usage, "_probe", return_value={"ok": True}) as probe:
            usage.read_usage()
            usage.read_usage()
        self.assertEqual(probe.call_count, 2)

    def test_each_cli_probes_its_own_and_says_which(self):
        with mock.patch.object(usage, "_probe", return_value={"ok": True, "limits": [1]}), \
             mock.patch.object(usage, "_probe_codex", return_value={"ok": True, "limits": [2]}):
            claude, codex = usage.read_usage("claude"), usage.read_usage("codex")
        self.assertEqual((claude["cli"], claude["limits"]), ("claude", [1]))
        self.assertEqual((codex["cli"], codex["limits"]), ("codex", [2]))

    def test_a_claude_probe_does_not_hold_up_codex(self):
        started = threading.Event()

        def slow_probe():
            started.set()
            time.sleep(0.5)
            return {"ok": True}

        with mock.patch.object(usage, "_probe", side_effect=slow_probe), \
             mock.patch.object(usage, "_probe_codex", return_value={"ok": True}):
            t = threading.Thread(target=usage.read_usage)
            t.start()
            started.wait(1)
            t0 = time.time()
            usage.read_usage("codex")
            self.assertLess(time.time() - t0, 0.3)
            t.join()

    def test_unknown_cli(self):
        r = usage.read_usage("gemini")
        self.assertFalse(r["ok"])
        self.assertEqual(r["cli"], "gemini")


class RouteTests(unittest.TestCase):
    def test_peer_host_is_forwarded(self):
        with mock.patch.object(appmod.peers, "configured", return_value={"b": "http://x"}), \
             mock.patch.object(appmod.peers, "forward", return_value={"ok": True}) as fwd, \
             mock.patch.object(appmod.usage, "read_usage") as local:
            appmod.api_usage(appmod.UsageBody(host="b"))
        local.assert_not_called()
        self.assertEqual(fwd.call_args[0], ("b", "POST", "/api/usage", {"cli": "claude"}))

    def test_peer_is_asked_for_the_cli_asked_for(self):
        with mock.patch.object(appmod.peers, "configured", return_value={"b": "http://x"}), \
             mock.patch.object(appmod.peers, "forward", return_value={"ok": True}) as fwd:
            appmod.api_usage(appmod.UsageBody(host="b", cli="codex"))
        self.assertEqual(fwd.call_args[0][3], {"cli": "codex"})

    def test_own_host_reads_the_cli_asked_for(self):
        with mock.patch.object(appmod.peers, "configured", return_value={}), \
             mock.patch.object(appmod.usage, "read_usage", return_value={"ok": True}) as local:
            appmod.api_usage(appmod.UsageBody(cli="codex"))
        local.assert_called_once_with("codex")

    def test_own_host_reads_here(self):
        with mock.patch.object(appmod.peers, "configured", return_value={"b": "http://x"}), \
             mock.patch.object(appmod.peers, "forward") as fwd, \
             mock.patch.object(appmod.usage, "read_usage", return_value={"ok": True}) as local:
            self.assertEqual(appmod.api_usage(appmod.UsageBody(host="")), {"ok": True})
        fwd.assert_not_called()
        local.assert_called_once()


# ── Codex ────────────────────────────────────────────────────────────────────

# account/rateLimits/read's result, as codex-cli 0.160.0 answers it, with a 5h
# window and a model limit of its own added.
_LIMITS_RESULT = {
    "ordinaryUsageAllowed": True,
    "rateLimits": {"limitId": "codex", "primary": {"usedPercent": 4, "windowDurationMins": 10080,
                                                    "resetsAt": 1791948547}},
    "rateLimitsByLimitId": {
        "codex_spark": {"limitId": "codex_spark", "limitName": "GPT-6-Spark",
                        "primary": {"usedPercent": 12.5, "windowDurationMins": 300, "resetsAt": 0},
                        "secondary": None},
        "codex": {"limitId": "codex", "limitName": None,
                  "primary": {"usedPercent": 31, "windowDurationMins": 300, "resetsAt": 1791400000},
                  "secondary": {"usedPercent": 4, "windowDurationMins": 10080,
                                "resetsAt": 1791948547},
                  "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
                  "planType": "promax"},
    },
}


class ParseCodexLimitsTests(unittest.TestCase):
    def test_plan_limits_first_then_a_models_own(self):
        rows = usage.parse_codex_limits(_LIMITS_RESULT)
        self.assertEqual([(r["label"], r["used"]) for r in rows],
                         [("5h limit", 31), ("Weekly limit", 4), ("GPT-6-Spark · 5h limit", 12.5)])
        self.assertTrue(rows[0]["resets"])
        self.assertEqual(rows[2]["resets"], "")  # no reset time given

    def test_only_the_plan_snapshot(self):
        rows = usage.parse_codex_limits({"rateLimits": _LIMITS_RESULT["rateLimits"]})
        self.assertEqual([(r["label"], r["used"]) for r in rows], [("Weekly limit", 4)])

    def test_a_window_without_a_percent_is_no_row(self):
        rows = usage.parse_codex_limits({"rateLimitsByLimitId": {"codex": {
            "primary": {"windowDurationMins": 300}, "secondary": None}}})
        self.assertEqual(rows, [])

    def test_window_labels(self):
        self.assertEqual([usage._window_label(m) for m in (300, 10080, 1440, 90, None)],
                         ["5h limit", "Weekly limit", "1d limit", "90m limit", "Limit"])

    def test_resets_in_local_time(self):
        self.addCleanup(time.tzset)  # after patch.dict has put TZ back
        with mock.patch.dict(os.environ, {"TZ": "America/Los_Angeles"}):
            time.tzset()
            self.assertEqual(usage._resets_text(1791948547), "Oct 13, 8:29pm (PDT)")
            self.assertEqual(usage._resets_text(1791946800), "Oct 13, 8pm (PDT)")
            self.assertEqual(usage._resets_text(0), "")


class ParseCodexAccountTests(unittest.TestCase):
    def test_chatgpt_login(self):
        self.assertEqual(usage.parse_codex_account(
            {"account": {"type": "chatgpt", "email": "a@b.c", "planType": "pro"}}),
            {"login": "ChatGPT pro", "email": "a@b.c"})

    def test_api_key(self):
        self.assertEqual(usage.parse_codex_account({"account": {"type": "apiKey"}}),
                         {"login": "API key", "email": ""})

    def test_logged_out(self):
        self.assertEqual(usage.parse_codex_account({"account": None}),
                         {"login": "", "email": ""})


class CliEnvTests(unittest.TestCase):
    def test_every_hop_of_the_link_chain_is_on_path(self):
        # ~/.local/bin/codex -> nvm/bin/codex -> lib/codex.js, with node in nvm/bin
        with tempfile.TemporaryDirectory() as d:
            local, nvm, lib = (Path(d) / p for p in ("local", "nvm", "lib"))
            for p in (local, nvm, lib):
                p.mkdir()
            (lib / "codex.js").write_text("")
            (nvm / "codex").symlink_to("../lib/codex.js")
            (local / "codex").symlink_to(nvm / "codex")
            with mock.patch.object(tmux, "_spawn_env", return_value={"PATH": "/usr/bin"}):
                path = usage._cli_env(str(local / "codex"))["PATH"].split(os.pathsep)
        self.assertEqual(path[0], "/usr/bin")
        self.assertIn(str(local), path)
        self.assertIn(str(nvm), path)

    def test_a_dir_already_on_path_is_not_added_again(self):
        with mock.patch.object(tmux, "_spawn_env", return_value={"PATH": "/opt/bin:/usr/bin"}):
            path = usage._cli_env("/opt/bin/codex")["PATH"]
        self.assertEqual(path, "/opt/bin:/usr/bin")


def _fake_codex(d: str, body: str) -> str:
    """An executable `codex` in `d` running `body` (Python) as its app-server."""
    exe = Path(d) / "codex"
    exe.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    exe.chmod(0o755)
    return str(exe)


# Answers as the real one does, and exits on stdin's EOF without answering what
# it was sent.
_ANSWERS = """
    import json, sys
    lines = []
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("id") == 1:
            print(json.dumps({"id": 1, "result": {}}), flush=True)
            print(json.dumps({"method": "remoteControl/status/changed", "params": {}}), flush=True)
        elif msg.get("id") == 2:
            print(json.dumps({"id": 2, "result": {"account": {
                "type": "chatgpt", "email": "a@b.c", "planType": "pro"}}}), flush=True)
        elif msg.get("id") == 3:
            print(json.dumps({"id": 3, "result": json.loads(%r)}), flush=True)
"""


class ProbeCodexTests(unittest.TestCase):
    def _probe(self, body: str) -> dict:
        with tempfile.TemporaryDirectory() as d:
            exe = _fake_codex(d, body)
            with mock.patch.object(tmux, "_resolve_cli", return_value=exe):
                return usage._probe_codex()

    def test_reads_account_and_limits(self):
        r = self._probe(_ANSWERS % json.dumps(_LIMITS_RESULT))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["account"], {"login": "ChatGPT pro", "email": "a@b.c"})
        self.assertEqual(r["limits"][0]["label"], "5h limit")

    def test_not_installed(self):
        with mock.patch.object(tmux, "_resolve_cli", return_value=None):
            r = usage._probe_codex()
        self.assertEqual((r["ok"], r.get("missing")), (False, True))

    def test_an_rpc_error_is_said(self):
        r = self._probe("""
            import json, sys
            for line in sys.stdin:
                msg = json.loads(line)
                if msg.get("id") in (2, 3):
                    print(json.dumps({"id": msg["id"], "error": {"code": -32600,
                          "message": "not logged in"}}), flush=True)
        """)
        self.assertEqual((r["ok"], r["error"]), (False, "codex: not logged in"))

    def test_one_that_dies_says_why(self):
        r = self._probe("""
            import sys
            sys.stderr.write("/usr/bin/env: 'node': No such file or directory\\n")
            sys.exit(127)
        """)
        self.assertFalse(r["ok"])
        self.assertIn("never answered", r["error"])
        self.assertIn("'node': No such file", r["error"])

    def test_a_silent_one_and_its_children_are_killed_at_the_deadline(self):
        # node running the native app-server as its child: the child holds the
        # pipe, so killing node alone would leave the reader waiting forever.
        with tempfile.TemporaryDirectory() as d:
            pidfile = Path(d) / "child.pid"
            exe = _fake_codex(d, f"""
                import subprocess, sys, time
                child = subprocess.Popen(["sleep", "60"])
                open({str(pidfile)!r}, "w").write(str(child.pid))
                time.sleep(60)
            """)
            with mock.patch.object(tmux, "_resolve_cli", return_value=exe), \
                 mock.patch.object(usage, "_CODEX_WAIT", 1.0):
                t0 = time.time()
                r = usage._probe_codex()
                took = time.time() - t0
            child = int(pidfile.read_text())
        self.assertFalse(r["ok"])
        self.assertLess(took, 5)
        for _ in range(20):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail("the app-server's child outlived it")

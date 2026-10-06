"""Tests for core/usage.py: reading plan usage off Claude's /status and /usage.

The panel fixtures are panes captured on v2.1.291: fixtures/usage_panel.txt once
the panel settled, fixtures/usage_panel_refreshing.txt the cached paint it shows
first (one limit short, "Refreshing…" below).
"""
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
        usage._last = None

    def tearDown(self):
        usage._last = None

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


class RouteTests(unittest.TestCase):
    def test_peer_host_is_forwarded(self):
        with mock.patch.object(appmod.peers, "configured", return_value={"b": "http://x"}), \
             mock.patch.object(appmod.peers, "forward", return_value={"ok": True}) as fwd, \
             mock.patch.object(appmod.usage, "read_usage") as local:
            appmod.api_usage(appmod.UsageBody(host="b"))
        local.assert_not_called()
        self.assertEqual(fwd.call_args[0][:3], ("b", "POST", "/api/usage"))

    def test_own_host_reads_here(self):
        with mock.patch.object(appmod.peers, "configured", return_value={"b": "http://x"}), \
             mock.patch.object(appmod.peers, "forward") as fwd, \
             mock.patch.object(appmod.usage, "read_usage", return_value={"ok": True}) as local:
            self.assertEqual(appmod.api_usage(appmod.UsageBody(host="")), {"ok": True})
        fwd.assert_not_called()
        local.assert_called_once()

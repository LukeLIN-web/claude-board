"""Tests for the multi-host layer: card addressing, peer merging, forwarding.

No network is touched — `core.peers._request` is the single seam every
board-to-board call goes through, so stubbing it exercises the polling, the
caching and the route forwarding without a second board.
"""
import os
import time
import unittest
from unittest import mock

import app as appmod
from core import peers


class ParseTests(unittest.TestCase):
    def test_parses_label_url_pairs(self):
        got = peers._parse_peers("b=http://127.0.0.1:7880, c=http://127.0.0.1:7881")
        self.assertEqual(got, {"b": "http://127.0.0.1:7880",
                               "c": "http://127.0.0.1:7881"})

    def test_skips_malformed_entries_instead_of_raising(self):
        """A typo in a per-host env file should cost one peer, not the board."""
        got = peers._parse_peers("b=http://a:1 nourl c=ftp://x b:2=http://b:2")
        self.assertEqual(got, {"b": "http://a:1"})


class SplitKeyTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(peers._peers)
        peers._peers.clear()
        peers._peers.update({"b": "http://127.0.0.1:7880"})
        self.addCleanup(lambda: (peers._peers.clear(), peers._peers.update(self._saved)))

    def test_bare_pid_is_local(self):
        self.assertEqual(peers.split_key("1234"), (None, 1234))

    def test_configured_peer_prefix_routes_to_that_host(self):
        self.assertEqual(peers.split_key("b:1234"), ("b", 1234))

    def test_unknown_prefix_falls_back_to_local(self):
        """A key minted by this host carries its own label; that must resolve
        here, not 404 because the label isn't in the peer list."""
        self.assertEqual(peers.split_key("a:1234"), (None, 1234))

    def test_non_numeric_pid_raises(self):
        with self.assertRaises(ValueError):
            peers.split_key("b:notapid")


class CacheTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(peers._peers)
        peers._peers.clear()
        peers._peers.update({"b": "http://127.0.0.1:7880"})
        peers._cache.clear()
        self.addCleanup(lambda: (peers._peers.clear(), peers._peers.update(self._saved),
                                 peers._cache.clear()))

    def _poll(self, payload, status=200):
        with mock.patch.object(peers, "_request", return_value=(status, payload)):
            peers._poll_once("b", "http://127.0.0.1:7880")

    def test_poll_stamps_host_and_key_on_each_card(self):
        self._poll({"windows": [{"pid": 42, "key": "42", "host": "b"}],
                    "tmux_available": True})
        (w,) = peers.remote_windows()
        self.assertEqual(w["host"], "b")
        self.assertEqual(w["key"], "b:42")
        self.assertFalse(w["peer_stale"])

    def test_stale_cards_are_marked_then_dropped(self):
        self._poll({"windows": [{"pid": 42}]})
        peers._cache["b"]["ts"] = time.time() - (peers.STALE_AFTER + 1)
        self.assertTrue(peers.remote_windows()[0]["peer_stale"])
        peers._cache["b"]["ts"] = time.time() - (peers.DROP_AFTER + 1)
        self.assertEqual(peers.remote_windows(), [],
                         "a board that went away must stop showing ghost cards")

    def test_a_failed_poll_keeps_the_last_good_cards(self):
        """A blip between polls shouldn't blank the peer's cards — only age
        eventually drops them."""
        self._poll({"windows": [{"pid": 42}]})
        with mock.patch.object(peers, "_request", side_effect=OSError("boom")):
            peers._poll_once("b", "http://127.0.0.1:7880")
        self.assertEqual(len(peers.remote_windows()), 1)
        self.assertIn("boom", peers.status()[0]["error"])

    def test_status_reports_an_unpolled_peer_as_offline(self):
        (row,) = peers.status()
        self.assertFalse(row["online"])
        self.assertEqual(row["host"], "b")


class ForwardTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(peers._peers)
        peers._peers.clear()
        peers._peers.update({"b": "http://127.0.0.1:7880"})
        self.addCleanup(lambda: (peers._peers.clear(), peers._peers.update(self._saved)))

    def test_forward_calls_the_peer_and_returns_its_body(self):
        with mock.patch.object(peers, "_request",
                               return_value=(200, {"ok": True})) as req:
            got = peers.forward("b", "POST", "/api/windows/42/prompt", {"text": "hi"})
        self.assertEqual(got, {"ok": True})
        url, method, body, _timeout = req.call_args[0]
        self.assertEqual(url, "http://127.0.0.1:7880/api/windows/42/prompt")
        self.assertEqual((method, body), ("POST", {"text": "hi"}))

    def test_unreachable_peer_reads_as_an_action_error(self):
        """The card renders `error`; a raised exception would leave the button
        silently dead instead."""
        with mock.patch.object(peers, "_request", side_effect=OSError("no route")):
            got = peers.forward("b", "POST", "/api/windows/42/close")
        self.assertFalse(got["ok"])
        self.assertIn("host b", got["error"], "the card has to say which host")

    def test_peer_http_error_is_passed_through_as_an_error(self):
        with mock.patch.object(peers, "_request",
                               return_value=(404, {"detail": "window not found"})):
            got = peers.forward("b", "POST", "/api/windows/42/close")
        self.assertFalse(got["ok"])
        self.assertIn("window not found", got["error"])

    def test_request_marks_itself_as_board_to_board(self):
        """Without this header a peer would answer with its own merged view and
        two boards pointed at each other would each show everything twice."""
        captured = {}

        class _Resp:
            status = 200
            def read(self): return b"{}"
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def _urlopen(req, timeout=None):
            captured["headers"] = dict(req.header_items())
            return _Resp()

        with mock.patch.dict(os.environ, {"FLEET_API_TOKEN": "tok"}), \
             mock.patch("urllib.request.urlopen", _urlopen):
            peers._request("http://x/api/windows", "GET", None, 1.0)
        headers = {k.lower(): v for k, v in captured["headers"].items()}
        self.assertEqual(headers[peers.PEER_HEADER.lower()], "1")
        self.assertEqual(headers["authorization"], "Bearer tok")


class RouteDispatchTests(unittest.TestCase):
    """The routes decide local-or-forward from the card key alone."""

    def setUp(self):
        self._saved = dict(peers._peers)
        peers._peers.clear()
        peers._peers.update({"b": "http://127.0.0.1:7880"})
        self.addCleanup(lambda: (peers._peers.clear(), peers._peers.update(self._saved)))

    def test_peer_key_forwards_the_action_untouched(self):
        with mock.patch.object(peers, "forward",
                               return_value={"ok": True}) as fwd, \
             mock.patch.object(appmod, "_require_window") as local:
            got = appmod.api_window_prompt("b:42", appmod.PromptBody(text="hi"))
        self.assertEqual(got, {"ok": True})
        local.assert_not_called()
        self.assertEqual(fwd.call_args[0][:3],
                         ("b", "POST", "/api/windows/42/prompt"))

    def test_local_key_never_leaves_the_host(self):
        with mock.patch.object(peers, "forward") as fwd, \
             mock.patch.object(appmod, "_require_window"), \
             mock.patch("core.actions.send_prompt", return_value={"ok": False}):
            appmod.api_window_prompt("42", appmod.PromptBody(text="hi"))
        fwd.assert_not_called()

    def test_malformed_key_is_a_404(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            appmod._split("nonsense")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_spawn_on_a_peer_is_forwarded_without_the_host_field(self):
        """The peer spawns locally; leaving `host` in the body would let a peer
        list that names this board back bounce the request around."""
        with mock.patch.object(peers, "forward", return_value={"ok": True}) as fwd:
            appmod.api_window_create(appmod.CreateBody(cwd="/tmp", host="b"))
        self.assertEqual(fwd.call_args[0][2], "/api/windows/create")
        self.assertEqual(fwd.call_args[0][3], {"cwd": "/tmp", "platform": "claude"})


class MergeTests(unittest.TestCase):
    def test_merged_snapshot_counts_and_orders_both_hosts(self):
        local = {
            "windows": [{"pid": 1, "key": "1", "host": "a", "triage": "completed",
                         "updated_at": 10}],
            "counts": {}, "ts": 0, "tmux_available": True, "host": "a",
        }
        remote = [{"pid": 1, "key": "b:1", "host": "b", "triage": "waiting_perm",
                   "updated_at": 5, "peer_stale": False}]
        with mock.patch.object(appmod, "_local_snapshot", return_value=local), \
             mock.patch.object(peers, "enabled", return_value=True), \
             mock.patch.object(peers, "remote_windows", return_value=remote), \
             mock.patch.object(peers, "status",
                               return_value=[{"host": "b", "online": True}]):
            snap = appmod._enriched_snapshot()
        self.assertEqual(snap["counts"]["total"], 2)
        self.assertEqual(snap["counts"]["waiting"], 1)
        # Most urgent first, regardless of which host it is on.
        self.assertEqual([w["key"] for w in snap["windows"]], ["b:1", "1"])

    def test_the_local_snapshot_handed_to_peers_never_grows_peer_cards(self):
        """It is served verbatim to a peer's poll; merging into it in place
        would feed a peer its own cards back."""
        local = {"windows": [{"pid": 1, "key": "1", "host": "a", "triage": "completed",
                              "updated_at": 10}],
                 "counts": {}, "ts": 0, "tmux_available": True, "host": "a"}
        remote = [{"pid": 9, "key": "b:9", "host": "b", "triage": "working",
                   "updated_at": 11}]
        with mock.patch.object(appmod, "_local_snapshot", return_value=local), \
             mock.patch.object(peers, "enabled", return_value=True), \
             mock.patch.object(peers, "remote_windows", return_value=remote), \
             mock.patch.object(peers, "status", return_value=[]):
            merged = appmod._enriched_snapshot()
        self.assertEqual(len(merged["windows"]), 2)
        self.assertEqual([w["key"] for w in appmod.state.last_local_snapshot["windows"]],
                         ["1"])

    def test_no_peers_configured_leaves_the_snapshot_untouched(self):
        local = {"windows": [], "counts": {}, "ts": 0, "tmux_available": False}
        with mock.patch.object(appmod, "_local_snapshot", return_value=local), \
             mock.patch.object(peers, "enabled", return_value=False):
            self.assertIs(appmod._enriched_snapshot(), local)


if __name__ == "__main__":
    unittest.main()

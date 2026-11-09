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


class _OnePeerTest(unittest.TestCase):
    """This board is configured with one peer, "b"."""

    def setUp(self):
        patch = mock.patch.dict(peers._peers, {"b": "http://127.0.0.1:7880"}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)


class SplitKeyTests(_OnePeerTest):

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


class CacheTests(_OnePeerTest):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(peers._cache, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

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

    def test_a_peer_stuck_mid_refresh_is_dimmed_but_kept(self):
        """It answers every poll at once, with cards it built long ago: they
        are marked stale from their build, not their arrival, and stay up —
        the sessions are real, only old."""
        self._poll({"windows": [{"pid": 42}], "age": peers.DROP_AFTER + 60})
        (w,) = peers.remote_windows()
        self.assertTrue(w["peer_stale"])
        (row,) = peers.status()
        self.assertTrue(row["online"])
        self.assertTrue(row["stale"])

    def test_a_peer_that_sends_no_usable_age_counts_as_fresh(self):
        """A board too old to send one, or a value that isn't an age."""
        for age in (None, "30", -5, True):
            self._poll({"windows": [{"pid": 42}], "age": age})
            self.assertFalse(peers.remote_windows()[0]["peer_stale"], age)
            self.assertFalse(peers.status()[0]["stale"], age)

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


class ForwardTests(_OnePeerTest):

    def test_forward_calls_the_peer_and_returns_its_body(self):
        with mock.patch.object(peers, "_request",
                               return_value=(200, {"ok": True})) as req:
            got = peers.forward_card("b", 42, "POST", "prompt", {"text": "hi"})
        self.assertEqual(got, {"ok": True})
        url, method, body, _timeout = req.call_args[0]
        self.assertEqual(url, "http://127.0.0.1:7880/api/windows/42/prompt")
        self.assertEqual((method, body), ("POST", {"text": "hi"}))

    def test_unreachable_peer_reads_as_an_action_error(self):
        """The card renders `error`; a raised exception would leave the button
        silently dead instead."""
        with mock.patch.object(peers, "_request", side_effect=OSError("no route")):
            got = peers.forward_card("b", 42, "POST", "close")
        self.assertFalse(got["ok"])
        self.assertIn("host b", got["error"], "the card has to say which host")

    def test_peer_http_error_is_passed_through_as_an_error(self):
        with mock.patch.object(peers, "_request",
                               return_value=(404, {"detail": "window not found"})):
            got = peers.forward_card("b", 42, "POST", "close")
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


class RouteDispatchTests(_OnePeerTest):
    """The routes decide local-or-forward from the card key alone."""

    def test_peer_key_forwards_the_action_untouched(self):
        with mock.patch.object(peers, "_request",
                               return_value=(200, {"ok": True})) as req, \
             mock.patch.object(appmod.sessions, "find_window") as local:
            got = appmod.api_window_prompt("b:42", appmod.PromptBody(text="hi"))
        self.assertEqual(got, {"ok": True})
        local.assert_not_called()
        self.assertEqual(req.call_args[0][:3],
                         ("http://127.0.0.1:7880/api/windows/42/prompt", "POST", {"text": "hi"}))

    def test_local_key_never_leaves_the_host(self):
        with mock.patch.object(peers, "_request") as fwd, \
             mock.patch.object(appmod.sessions, "find_window"), \
             mock.patch("core.actions.send_prompt", return_value={"ok": False}):
            appmod.api_window_prompt("42", appmod.PromptBody(text="hi"))
        fwd.assert_not_called()

    def test_malformed_key_is_a_404(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            appmod.api_close("nonsense")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_spawn_on_a_peer_is_forwarded_without_the_host_field(self):
        """The peer spawns locally; leaving `host` in the body would let a peer
        list that names this board back bounce the request around."""
        with mock.patch.object(peers, "_request", return_value=(200, {"ok": True})) as req:
            appmod.api_window_create(appmod.CreateBody(cwd="/tmp", host="b"))
        self.assertEqual(req.call_args[0][0], "http://127.0.0.1:7880/api/windows/create")
        self.assertEqual(req.call_args[0][2], {"cwd": "/tmp", "platform": "claude"})


class MergeTests(unittest.TestCase):
    def test_merged_snapshot_counts_and_orders_both_hosts(self):
        local = {
            "windows": [{"pid": 1, "key": "1", "host": "a", "triage": "completed",
                         "updated_at": 10}],
            "gpus": [], "counts": {}, "ts": 0, "tmux_available": True, "host": "a",
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
                 "gpus": [], "counts": {}, "ts": 0, "tmux_available": True, "host": "a"}
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


class _UpstreamTest(unittest.TestCase):
    """This board is "a6000": no direct peers but "b", and an upstream "71"
    that shows 71, 63, b and a6000 itself."""

    UP = "http://127.0.0.1:50957"

    def setUp(self):
        for patch in (mock.patch.dict(peers._peers, {"b": "http://127.0.0.1:7880"}, clear=True),
                      mock.patch.object(peers, "_upstream", self.UP),
                      mock.patch.object(peers, "local_label", return_value="a6000"),
                      mock.patch.dict(peers._cache, clear=True),
                      mock.patch.dict(peers._polled_by, clear=True)):
            patch.start()
            self.addCleanup(patch.stop)

    def poll_upstream(self):
        view = {
            "host": "71",
            "hosts": ["71", "63", "b", "a6000"],
            "windows": [
                {"pid": 1, "key": "71:1", "host": "71"},
                {"pid": 2, "key": "63:2", "host": "63"},
                {"pid": 3, "key": "b:3", "host": "b"},          # reached directly here
                {"pid": 4, "key": "a6000:4", "host": "a6000"},  # this board's own
            ],
            "gpus": [{"host": "63", "index": 0}, {"host": "a6000", "index": 0}],
            "peers": [{"host": "63", "online": False, "error": "HTTP 502"}],
        }
        with mock.patch.object(peers, "_request", return_value=(200, view)) as req:
            peers._poll_once(peers._UPSTREAM, self.UP)
        return req


class UpstreamTests(_UpstreamTest):

    def test_upstream_is_read_with_the_view_header(self):
        req = self.poll_upstream()
        self.assertTrue(req.call_args.kwargs.get("view"))

    def test_merges_only_hosts_this_board_cannot_reach_itself(self):
        """Its own cards and a direct peer's would otherwise show twice."""
        self.poll_upstream()
        keys = sorted(w["key"] for w in peers.remote_windows())
        self.assertEqual(keys, ["63:2", "71:1"])
        self.assertEqual([g["host"] for g in peers.remote_gpus()], ["63"])

    def test_upstream_card_routes_through_the_upstream_by_full_key(self):
        self.poll_upstream()
        self.assertEqual(peers.split_key("63:2"), ("63", 2))
        with mock.patch.object(peers, "_request", return_value=(200, {"ok": True})) as req:
            peers.forward_card("63", 2, "POST", "prompt", {"text": "hi"})
        self.assertEqual(req.call_args[0][0], f"{self.UP}/api/windows/63:2/prompt")

    def test_own_label_and_direct_peer_never_detour(self):
        self.poll_upstream()
        self.assertEqual(peers.split_key("a6000:4"), (None, 4))
        with mock.patch.object(peers, "_request", return_value=(200, {"ok": True})) as req:
            peers.forward_card("b", 3, "POST", "close")
        self.assertEqual(req.call_args[0][0], "http://127.0.0.1:7880/api/windows/3/close")

    def test_host_route_through_upstream_keeps_the_host(self):
        """The upstream needs it to know where to send the request on."""
        self.poll_upstream()
        with mock.patch.object(peers, "_request", return_value=(200, {"ok": True})) as req:
            peers.forward_host("63", "/api/usage", {"host": "63", "cli": "claude"})
        self.assertEqual(req.call_args[0][:3],
                         (f"{self.UP}/api/usage", "POST", {"host": "63", "cli": "claude"}))

    def test_host_unknown_everywhere_runs_here(self):
        self.poll_upstream()
        self.assertFalse(peers.is_peer("99"))

    def test_status_rows_for_upstream_hosts_carry_the_upstreams_view(self):
        self.poll_upstream()
        rows = {r["host"]: r for r in peers.status()}
        self.assertEqual(set(rows), {"b", "63", "71"})
        self.assertEqual(rows["71"]["via"], "71")
        self.assertTrue(rows["71"]["online"])
        self.assertFalse(rows["63"]["online"], "the upstream says 63 is down")
        self.assertEqual(rows["63"]["error"], "HTTP 502")

    def test_unanswered_upstream_is_one_offline_row(self):
        rows = [r for r in peers.status() if r.get("via")]
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["online"])


class FleetViewTests(_UpstreamTest):
    """What this board answers a downstream board that reads it as upstream."""

    def test_local_cards_are_keyed_by_host_and_upstream_cards_left_out(self):
        self.poll_upstream()
        with mock.patch.object(peers, "_request",
                               return_value=(200, {"windows": [{"pid": 9}], "gpus": []})):
            peers._poll_once("b", "http://127.0.0.1:7880")
        local = {"host": "a6000", "windows": [{"pid": 5, "key": "5", "host": "a6000"}],
                 "gpus": []}
        view = peers.fleet_view(local)
        self.assertEqual(sorted(w["key"] for w in view["windows"]), ["a6000:5", "b:9"])
        self.assertEqual(view["hosts"], ["a6000", "b"])
        self.assertEqual(local["windows"][0]["key"], "5", "the local snapshot is not mutated")

    def test_api_windows_answers_the_view_and_notes_the_poller(self):
        local = {"host": "a6000", "windows": [{"pid": 5, "key": "5", "host": "a6000"}],
                 "gpus": [], "ts": 1.0}
        req = mock.Mock()
        req.headers = {peers.PEER_HEADER: "1", peers.VIEW_HEADER: "1", peers.FROM_HEADER: "x1"}
        with mock.patch.object(appmod.state, "last_local_snapshot", local):
            got = appmod.api_windows(req, mock.Mock())
        self.assertEqual([w["key"] for w in got["windows"]], ["a6000:5"])
        self.assertEqual(peers.polled_by(), ["x1"])

    def test_plain_peer_poll_still_gets_the_local_half(self):
        local = {"host": "a6000", "windows": [{"pid": 5, "key": "5", "host": "a6000"}],
                 "gpus": [], "ts": 1.0}
        req = mock.Mock()
        req.headers = {peers.PEER_HEADER: "1"}
        with mock.patch.object(appmod.state, "last_local_snapshot", local):
            got = appmod.api_windows(req, mock.Mock())
        self.assertEqual([w["key"] for w in got["windows"]], ["5"])

    def test_polled_by_forgets_old_pollers(self):
        peers.note_polled_by("x1")
        peers._polled_by["x1"] -= peers.POLLED_FOR + 1
        self.assertEqual(peers.polled_by(), [])

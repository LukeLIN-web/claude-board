"""board-send.py's target matching (key, then name, then session-id prefix) and how it
names the session that sent the line."""
import importlib.util
import os
import unittest
from unittest import mock
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "board_send", Path(__file__).resolve().parent.parent / "scripts" / "board-send.py")
bs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bs)

WINS = [
    {"key": "63:124275", "name": "agent-harness-dispatcher", "session_id": "79d5d1aa-b3ca"},
    {"key": "71:5", "name": "agent-harness-w19", "session_id": "79d5d1ff-0000"},
    {"key": "6", "name": "63:124275", "session_id": "aaaaaaaa"},
]


class MatchTests(unittest.TestCase):
    def test_key_wins_over_a_name_that_looks_like_it(self):
        self.assertEqual([w["key"] for w in bs.match(WINS, "63:124275")], ["63:124275"])

    def test_name(self):
        self.assertEqual([w["key"] for w in bs.match(WINS, "agent-harness-w19")], ["71:5"])

    def test_session_id_prefix(self):
        self.assertEqual([w["key"] for w in bs.match(WINS, "79d5d1aa-")], ["63:124275"])

    def test_ambiguous_prefix_returns_all_so_the_caller_refuses(self):
        self.assertEqual(len(bs.match(WINS, "79d5d1")), 2)

    def test_short_prefix_matches_nothing(self):
        self.assertEqual(bs.match(WINS, "79d5"), [])


class SenderTests(unittest.TestCase):
    CARDS = [
        {"key": "70", "pid": 70, "name": "outer-codex", "session_id": "cx-1"},
        {"key": "71", "pid": 71, "name": "inner-claude", "session_id": "cc-1"},
        {"key": "63:71", "pid": 71, "name": "same-pid-other-host", "session_id": "cc-9"},
        {"key": "72", "pid": 72, "name": None, "session_id": "cc-2"},
    ]

    def sender(self, ancestors, env):
        with mock.patch.object(bs, "ancestors", return_value=ancestors), \
             mock.patch.dict(os.environ, env, clear=True):
            return bs.sender(self.CARDS)

    def test_the_nearest_ancestor_card_with_an_inherited_session_id(self):
        self.assertEqual(self.sender([500, 71, 70, 1], {"CLAUDE_CODE_SESSION_ID": "cc-1", "CODEX_THREAD_ID": "cx-1"}),
                         "inner-claude")

    def test_a_stale_inherited_id_whose_card_is_no_ancestor_names_nobody(self):
        self.assertEqual(self.sender([500, 900], {"CLAUDE_CODE_SESSION_ID": "cc-1"}), "")

    def test_an_unnamed_card_is_named_by_its_key(self):
        self.assertEqual(self.sender([72], {"CLAUDE_CODE_SESSION_ID": "cc-2"}), "72")

    def test_outside_any_session_nobody(self):
        self.assertEqual(self.sender([500, 71], {}), "")


class AncestorsTest(unittest.TestCase):
    def test_starts_at_the_parent(self):
        self.assertEqual(bs.ancestors()[0], os.getppid())

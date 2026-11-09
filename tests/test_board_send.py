"""board-send.py's target matching: key, then name, then session-id prefix."""
import importlib.util
import unittest
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

"""Tests for the you-should-know suggestion scrape (core.actions.parse_pane_tip).

The captures are laid out from the plugin's render code (Claude Code 2.1.317),
not taken from a live pane: the plugin offers on its own schedule and two probe
turns in a haiku session drew none. Replace them with a real capture once one
lands in .live-captures/."""
import unittest

from core import actions

_BOX = """\
────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
  ⏵⏵ auto mode on (shift+tab to cycle) · ← 2 agents
"""

CAP_OFFER = """\
● Encryption at rest is on for the Orders DB.

✻ Cooked for 40s · done 7:45 AM

✦ You should know · Orders DB encryption uses extra CPU, which can slow the
  nightly export
  1: Learn more  2: Knew this already  4: Disable  0: Dismiss
""" + _BOX

CAP_OFFER_UNDER_QUEUE = """\
✶ Noodling… (3s · ↓ 25 tokens)

✦ You should know · One-off /ask questions now cost more
  1: Learn more  2: Knew this already  0: Dismiss
  ❯ second queued message here
""" + _BOX

CAP_NONE = """\
✻ Sautéing… (46s · ↑ 1.9k tokens)
  ⎿  Tip: Use /memory to view and manage Claude memory

""" + _BOX

# A ✦ the transcript scrolled past is not a live suggestion.
CAP_STALE_ABOVE_OUTPUT = """\
✦ You should know · an old one
  1: Learn more  2: Knew this already  0: Dismiss

● Next answer.
""" + _BOX


class ParsePaneTipTests(unittest.TestCase):
    def test_wrapped_offer_joins_lines_and_drops_buttons(self):
        self.assertEqual(
            actions.parse_pane_tip(CAP_OFFER),
            "You should know · Orders DB encryption uses extra CPU, which can "
            "slow the nightly export")

    def test_queued_messages_in_the_block_drop(self):
        self.assertEqual(actions.parse_pane_tip(CAP_OFFER_UNDER_QUEUE),
                         "You should know · One-off /ask questions now cost more")

    def test_no_suggestion(self):
        self.assertIsNone(actions.parse_pane_tip(CAP_NONE))

    def test_suggestion_only_in_scrollback(self):
        self.assertIsNone(actions.parse_pane_tip(CAP_STALE_ABOVE_OUTPUT))

    def test_empty_and_boxless(self):
        self.assertIsNone(actions.parse_pane_tip(""))
        self.assertIsNone(actions.parse_pane_tip("✦ You should know · x\n"))


if __name__ == "__main__":
    unittest.main()

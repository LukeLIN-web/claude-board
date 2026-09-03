"""The model readout's second source: the welcome banner in the pane.

The transcript only names a model on assistant rows, so a session that has not
answered yet — freshly launched, or freshly /cleared, which starts a whole new
transcript — has nothing to read there. The banner Claude paints at both of
those moments is the only other place the session says what it is running on.
"""
import unittest
from unittest import mock

from core import actions


# A real 80-column capture: the version line, the model line, the cwd line.
BANNER = """\
 ▐▛███▛█   Claude Code v2.1.259
▝▜██████▀  Opus 5 with xhigh effort · Claude Max
  ▝▝ ▝▝    /shared/user75/workspace/juyi/qwen3omni

⚠ 1 MCP server needs authentication · run /mcp

❯
"""


class BannerModelTests(unittest.TestCase):
    def test_reads_the_model_line(self):
        self.assertEqual(actions.banner_model(BANNER), "Opus 5")

    def test_effort_and_plan_are_not_part_of_the_model(self):
        # Both change without the model changing; neither belongs on the card.
        self.assertNotIn("effort", actions.banner_model(BANNER))
        self.assertNotIn("Claude Max", actions.banner_model(BANNER))

    def test_context_qualifier_is_dropped(self):
        # The transcript records "claude-opus-5" for a 1M session too, so keeping
        # the banner's "(1M context)" would make the label change on the
        # session's first reply — which reads exactly like a model switch.
        text = BANNER.replace("Opus 5 with", "Opus 5 (1M context) with")
        self.assertEqual(actions.banner_model(text), "Opus 5")

    def test_model_line_without_an_effort_suffix(self):
        text = BANNER.replace("Opus 5 with xhigh effort · Claude Max",
                              "Sonnet 4.5 · Claude Pro")
        self.assertEqual(actions.banner_model(text), "Sonnet 4.5")

    def test_last_banner_wins(self):
        # /clear reprints the banner, so a session cleared after a /model switch
        # carries the model it used to be on ABOVE the one it is on now.
        text = BANNER + BANNER.replace("Opus 5", "Fable 5.1")
        self.assertEqual(actions.banner_model(text), "Fable 5.1")

    def test_survives_a_repainted_model_line(self):
        # tmux capture of a redrawing pane can double the line; the first one
        # that parses answers, and both say the same thing anyway.
        lines = BANNER.splitlines()
        text = "\n".join(lines[:2] + [lines[1]] + lines[2:])
        self.assertEqual(actions.banner_model(text), "Opus 5")

    def test_cwd_line_is_not_a_model(self):
        # Drop the model line: the path under it must not be read as one.
        text = "\n".join(l for l in BANNER.splitlines() if "Opus" not in l)
        self.assertEqual(actions.banner_model(text), "")

    def test_no_banner_at_all(self):
        self.assertEqual(actions.banner_model("❯ hello\n● working…\n"), "")

    def test_empty_capture(self):
        self.assertEqual(actions.banner_model(""), "")


class PaneModelTests(unittest.TestCase):
    def test_reads_the_pane_with_scrollback(self):
        # A session that has typed anything since it was cleared has already
        # pushed the banner above the fold, so the visible viewport alone is not
        # enough to answer for it.
        with mock.patch.object(actions.tmux, "pane_for_tty", return_value="%5"), \
             mock.patch.object(actions.tmux, "capture_pane",
                               return_value={"ok": True, "text": BANNER}) as cap:
            self.assertEqual(actions.pane_model("/dev/pts/3"), "Opus 5")
        self.assertGreater(cap.call_args.kwargs.get("scrollback", 0), 0)

    def test_no_tty(self):
        self.assertEqual(actions.pane_model(None), "")
        self.assertEqual(actions.pane_model(""), "")

    def test_session_not_in_a_pane(self):
        with mock.patch.object(actions.tmux, "pane_for_tty", return_value=None):
            self.assertEqual(actions.pane_model("/dev/pts/3"), "")

    def test_failed_capture_claims_nothing(self):
        with mock.patch.object(actions.tmux, "pane_for_tty", return_value="%5"), \
             mock.patch.object(actions.tmux, "capture_pane",
                               return_value={"ok": False, "error": "no pane", "text": ""}):
            self.assertEqual(actions.pane_model("/dev/pts/3"), "")


if __name__ == "__main__":
    unittest.main()

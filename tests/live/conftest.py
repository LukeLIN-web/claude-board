"""Keep the pane a live test failed on.

A live failure is almost always Claude Code drawing something the board's
parsers don't know yet. That screen is what the fix needs: cut a fixture from
it and the unit tests hold the new shape from then on, with no session needed.
So a failed test writes the pane, with scrollback, to
.live-captures/<claude version>/<test>.txt (gitignored) and prints its tail.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core import tmux

_CAPTURES = Path(__file__).resolve().parents[2] / ".live-captures"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if rep.when != "call" or not rep.failed:
        return
    # Whichever session fixture the test drove: anything with a pane and a version.
    sessions = [v for v in getattr(item, "funcargs", {}).values()
                if hasattr(v, "pane") and hasattr(v, "version")]
    for s in sessions:
        text = tmux.capture_pane(s.pane, scrollback=200).get("text", "")
        dest = _CAPTURES / s.version / f"{item.name}.txt"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
        tail = "\n".join(text.rstrip().splitlines()[-40:])
        rep.sections.append(("pane at failure", f"saved to {dest}\n\n{tail}"))

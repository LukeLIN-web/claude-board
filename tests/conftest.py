"""What every run of the suite shares: the home it reads, and the live gate.

A plain run never sees the real home directory. The board reads ~/.claude and
~/.codex, and some modules fix their paths at import (core/btwlog.py, the
HOME_BASE in core/sessions.py), so HOME and CLAUDE_FLEET_HOME are pointed at an
empty temporary directory here, in pytest_configure, before collection imports
any of them. A test that forgets to stub a path then finds nothing, and never the
sessions of whoever ran it. The same goes for tmux: FLEET_TMUX_SOCKET names a
private, empty server for the run. The temp home is removed when the run ends,
so files a test writes under it need no cleanup of their own.

`--run-live` is the other way round: it runs tests/live and nothing else, against
the real home, because those tests drive a real Claude Code session and the board
has to find that session where Claude writes it. Without the flag those tests
skip, which is what CI does.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

_fake_home: str | None = None
_tmux_socket: str | None = None


def pytest_addoption(parser):
    parser.addoption(
        "--run-live", action="store_true",
        help="run tests/live alone: spawn a haiku Claude Code session in a "
             "private tmux server and drive it through the board (spends tokens)",
    )


def pytest_configure(config):
    global _fake_home, _tmux_socket
    if config.getoption("--run-live"):
        # The live tests find their session under the real ~/.claude.
        os.environ.pop("CLAUDE_FLEET_HOME", None)
        return
    _fake_home = tempfile.mkdtemp(prefix="claude-board-tests-")
    os.environ["HOME"] = _fake_home
    os.environ["CLAUDE_FLEET_HOME"] = _fake_home
    # Where tests/helpers.py makes its scratch files: named apart from HOME so a
    # test module run without this conftest writes to the system temp dir, never
    # into whatever real home it found.
    os.environ["CLAUDE_BOARD_TEST_HOME"] = _fake_home
    # The tmux server, likewise: core/tmux.py reads FLEET_TMUX_SOCKET on every
    # call, so a tmux call a test forgot to stub goes to a private server with no
    # panes — never to the one the user's real sessions (or a live board) run on.
    _tmux_socket = f"claude-board-tests-{os.getpid()}"
    os.environ["FLEET_TMUX_SOCKET"] = _tmux_socket


def pytest_unconfigure(config):
    if _fake_home:
        shutil.rmtree(_fake_home, ignore_errors=True)
    if _tmux_socket and shutil.which("tmux"):
        # An unstubbed `start-server` would have left that private server running.
        subprocess.run(["tmux", "-L", _tmux_socket, "kill-server"],
                       capture_output=True, check=False)


def _is_live(item) -> bool:
    # The marker, not `item.keywords`: keywords also hold the names of a test's
    # parents, so everything under tests/live would answer to "live" marked or not.
    return item.get_closest_marker("live") is not None


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-live"):
        # Only the live tier: everything else expects the fake home above.
        others = [i for i in items if not _is_live(i)]
        if others:
            config.hook.pytest_deselected(items=others)
            items[:] = [i for i in items if _is_live(i)]
        return
    skip = pytest.mark.skip(reason="drives a real Claude Code session; run with --run-live")
    for item in items:
        if _is_live(item):
            item.add_marker(skip)

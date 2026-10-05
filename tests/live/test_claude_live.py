"""Drive a real Claude Code session through the board, end to end.

CI cannot check this part: whether the board still reads and drives the Claude
Code installed on this machine. Claude Code redraws its screens between releases
(the folder-trust prompt, the welcome banner, the Rewind panel, the /model
dialog), and every parser in core/actions.py was written against one release's
text. These tests take sessions through the board's own code paths:

  a spawn into a never-trusted directory gets its trust prompt answered;
  then one session, in file order: card found → banner read → prompt lands and
  is answered → a prompt sent over an open Rewind panel lands → the /model
  dialog commits a pick.

Per CLAUDE.md, sessions run on haiku and every prompt here goes to one of them.
They run on a tmux server of their own (`tmux -L claude-board-live-<pid>`), so
nothing here can type into a pane someone is working in, and that server is
killed at the end. The run costs two short haiku turns.

    pytest tests/live --run-live -v

When a test fails, tests/live/conftest.py saves the pane to .live-captures/.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path

import pytest

from core import actions, sessions, tmux, transcripts

pytestmark = [pytest.mark.live, pytest.mark.timeout(300)]

_SOCKET = f"claude-board-live-{os.getpid()}"


@dataclass
class Live:
    pane: str
    pid: int
    tty: str
    cwd: str
    version: str
    trust: dict


def _wait_for(probe, timeout: float, interval: float = 0.5):
    """Poll `probe` until it returns something truthy; return that, or None."""
    deadline = time.time() + timeout
    while True:
        got = probe()
        if got:
            return got
        if time.time() >= deadline:
            return None
        time.sleep(interval)


def _pane_text(live: Live) -> str:
    return tmux.capture_pane(live.pane).get("text", "")


def _display(pane: str, fmt: str) -> str:
    r = tmux._run("display-message", "-p", "-t", pane, fmt)
    return r["stdout"].strip() if r["ok"] else ""


def _composer(live: Live) -> str:
    text = _pane_text(live)
    return text if "❯" in text and not actions._trust_prompt_painting(text) else ""


@pytest.fixture(scope="module")
def version():
    if not shutil.which("tmux"):
        pytest.skip("tmux is not installed")
    claude = tmux._resolve_cli("claude")
    if not claude:
        pytest.skip("claude is not installed")
    out = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=60)
    return (out.stdout.split() or ["unknown"])[0]


@pytest.fixture(scope="module")
def private_tmux(version):
    """Point every tmux call the board makes at a server only these tests use."""
    mp = pytest.MonkeyPatch()
    mp.setenv("FLEET_TMUX_SOCKET", _SOCKET)
    for k in ("FLEET_TMUX_SESSION", "TMUX", "CLAUDE_FLEET_CWD_INCLUDE", "CLAUDE_FLEET_CWD_EXCLUDE"):
        mp.delenv(k, raising=False)
    sessions._reload_cwd_filters()
    tmux._clear_caches()
    try:
        yield
    finally:
        tmux._run("kill-server")
        sock = os.path.join(os.environ.get("TMUX_TMPDIR") or "/tmp", f"tmux-{os.getuid()}", _SOCKET)
        if os.path.exists(sock):
            os.unlink(sock)
        mp.undo()
        sessions._reload_cwd_filters()
        tmux._clear_caches()


def _spawn(cwd: str, version: str) -> Live:
    # What create_session does, with the model pinned to haiku.
    r = tmux.new_window(cwd, ["claude", "--dangerously-skip-permissions", "--model", "haiku"])
    assert r["ok"], f"spawn failed: {r}"
    pane = r["pane_id"]
    trust = actions.confirm_trust_prompt(pane)
    return Live(pane=pane, pid=int(_display(pane, "#{pane_pid}") or 0),
                tty=_display(pane, "#{pane_tty}"), cwd=cwd, version=version, trust=trust)


def _close(live: Live) -> None:
    tmux._run("kill-pane", "-t", live.pane)
    # Claude's exit hooks may still write into the cwd; let it finish first.
    if live.pid:
        _wait_for(lambda: not sessions._pid_alive(live.pid), 15)
    shutil.rmtree(live.cwd, ignore_errors=True)


@pytest.fixture
def untrusted(private_tmux, version):
    """A session spawned into a directory no one has trusted, so Claude asks.

    /var/tmp rather than /tmp: trust is inherited from any trusted ancestor, and
    /tmp is one a person is likely to have trusted. No prompt is sent to it."""
    parent = "/var/tmp" if os.access("/var/tmp", os.W_OK) else None
    s = _spawn(tempfile.mkdtemp(prefix="claude-board-live-", dir=parent), version)
    yield s
    _close(s)


@pytest.fixture(scope="module")
def live(private_tmux, version):
    """The session the rest of the tests drive, in order."""
    s = _spawn(tempfile.mkdtemp(prefix="claude-board-live-"), version)
    try:
        if not _wait_for(lambda: _composer(s), 20):
            pytest.skip(f"the session never reached a composer (trust: {s.trust}); "
                        "see test_spawn_answers_the_trust_prompt")
        yield s
    finally:
        _close(s)


def test_spawn_answers_the_trust_prompt(untrusted):
    trust = untrusted.trust
    if trust.get("reason") == "already trusted":
        warnings.warn(f"{untrusted.cwd} was already trusted; the trust prompt went unexercised")
    else:
        assert trust.get("answered"), f"the board left the trust prompt up: {trust}"
    assert _wait_for(lambda: _composer(untrusted), 20), "no composer after the trust prompt"


def test_board_finds_the_session(live):
    w = _wait_for(lambda: sessions.find_window(live.pid), 20)
    assert w is not None, f"no card for pid {live.pid}"
    assert w.platform == "claude"
    assert w.tty and tmux.pane_for_tty(w.tty) == live.pane


def test_banner_names_haiku(live):
    label = actions.pane_model(live.tty)
    assert "haiku" in label.lower(), f"banner model read as {label!r}"


def _answered(live: Live, nonce: str):
    w = sessions.find_window(live.pid)
    path = w and w.transcript_path
    if not path or not Path(path).exists():
        return None
    for e in transcripts.timeline(path, limit=50):
        if e["kind"] == "assistant_text" and nonce in e["text"]:
            return path
    return None


def _ask(live: Live) -> str:
    """Send one probe through the board, wait for haiku's answer; the transcript path."""
    nonce = f"PONG{uuid.uuid4().hex[:6].upper()}"
    r = actions.send_prompt(live.pid, f"Reply with only the word {nonce} and nothing else. Use no tools.")
    assert r.get("ok"), f"send failed: {r}"
    path = _wait_for(lambda: _answered(live, nonce), 120, 1.0)
    assert path, f"no answer with {nonce} in the session's transcript"
    return path


def test_prompt_lands_and_is_answered(live):
    path = _ask(live)
    assert "haiku" in transcripts.current_model(path).lower()
    w = _wait_for(lambda: (w := sessions.find_window(live.pid)) and w.status == "idle" and w, 30)
    assert w, f"card status stayed {sessions.find_window(live.pid).status!r} after the answer"


def test_send_clears_an_open_rewind_panel(live):
    # Esc Esc on an idle composer opens the Rewind panel, which eats typed text;
    # send_prompt has to recognise it and close it first.
    tmux.send_keys(live.pane, "Escape")
    time.sleep(0.3)
    tmux.send_keys(live.pane, "Escape")
    assert _wait_for(lambda: actions._rewind_panel_open(_pane_text(live)), 5), \
        "Esc Esc did not draw a Rewind panel the board recognises"
    _ask(live)


def test_model_dialog_commits_a_pick(live):
    # Last on purpose: no prompt follows it, so whatever it picks, nothing is
    # sent to a model other than haiku.
    r = actions.switch_model(live.pid, "haiku")
    assert r.get("ok"), f"switch failed: {r}"
    assert "haiku" in r["model"].lower()
    assert actions._model_dialogs_closed(_pane_text(live))

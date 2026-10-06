"""This machine's Claude plan usage, read off /status and /usage.

How much of the current session and of the week is gone, and when each resets,
is drawn by Claude Code's /usage panel and kept nowhere on disk. So the board
asks Claude the way a person would: it starts a throwaway `claude` on a tmux
server of its own, reads the account off /status and the limits off /usage, and
kills the server. Neither command reaches the model. The session runs on haiku
anyway, so a keystroke that went astray costs a haiku turn and nothing more.

The probe runs in sessions.PROBE_CWD, which discovery never cards, and on a
server no user attaches to, so nothing about it shows on the board or in tmux.

An account is logged in per machine, so each board answers for its own host; a
peer's usage is read by the peer's board (app.py forwards the request).
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Optional

from . import actions, tmux
from .sessions import PROBE_CWD

# Tall enough for the Usage tab's limit rows below its per-session block: at a
# detached pane's 80x24 the third row is under the fold.
_COLS, _ROWS = 120, 50

_START_WAIT = 30.0   # a cold start with MCP servers to connect can be slow
_PANEL_WAIT = 8.0
# The panel first paints the last numbers Claude cached, with "Refreshing…"
# under them, then repaints with fresh ones — and the cached paint can lack a
# row (a model's weekly limit) the fresh one has.
_USAGE_WAIT = 15.0
_POLL = 0.2

_MARKER = tmux.composer_marker("claude")

# The settings dialog both commands open: "Settings  Status   Config   Usage   Stats".
_TABBAR_RE = re.compile(r"\bStatus\s+Config\s+Usage\b")
_REFRESHING = "Refreshing"

# A limit is a heading, a bar ending in "N% used", and a "Resets …" line:
#   Current week (all models)
#   ███████████▌                                       23% used
#   Resets Oct 11, 7pm (America/Los_Angeles)
_USED_RE = re.compile(r"(\d+(?:\.\d+)?)%\s+used\s*$")
_RESETS_RE = re.compile(r"^\s*Resets\s+(\S.*?)\s*$")
_BAR_CHARS = "█▉▊▋▌▍▎▏  \t"

_ACCOUNT_RE = re.compile(r"^\s*(Login method|Email):\s+(\S.*?)\s*$", re.M)

_lock = threading.Lock()
_last: Optional[dict] = None


def parse_limits(text: str) -> list[dict]:
    """[{label, used, resets}] for each limit on the Usage tab, in panel order.
    `used` is the percent; `resets` is Claude's own wording, "" if it drew none."""
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        m = _USED_RE.search(line)
        if not m:
            continue
        # The heading sits on the line above the bar — or, squeezed, before it.
        label = line[:m.start()].strip(_BAR_CHARS) or next(
            (ln.strip() for ln in reversed(lines[:i]) if ln.strip()), "")
        after = next((ln for ln in lines[i + 1:] if ln.strip()), "")
        resets = _RESETS_RE.match(after)
        used = float(m.group(1))
        out.append({"label": label, "used": int(used) if used.is_integer() else used,
                    "resets": resets.group(1) if resets else ""})
    return out


def usage_settled(text: str) -> bool:
    """Whether the Usage tab shows its limits and is done refreshing them."""
    return bool(parse_limits(text)) and _REFRESHING not in text


def parse_account(text: str) -> dict:
    """{login, email} off the Status tab; "" for a field it doesn't show."""
    fields = dict(_ACCOUNT_RE.findall(text))
    return {"login": fields.get("Login method", ""), "email": fields.get("Email", "")}


def _panel_says(text: str) -> str:
    """What the Usage tab shows below its per-session block, on one line. It is
    the reason when there are no limits to read: an account with no plan, say."""
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if _TABBAR_RE.search(ln)), None)
    if start is None:
        return ""
    body = [ln.strip() for ln in lines[start + 1:] if ln.strip()]
    if "Esc to cancel" in body:
        body = body[:body.index("Esc to cancel")]
    # The block ends at its token line: "Usage:  0 input, 0 output, …".
    tail = next((i for i, ln in enumerate(body) if ln.startswith("Usage:")), -1)
    return " · ".join(body[tail + 1:][:3])


def _at_composer(text: str) -> bool:
    return _MARKER in text and not actions._trust_prompt_painting(text)


def _panel_open(text: str) -> bool:
    return bool(_TABBAR_RE.search(text))


# Enters a panel command gets: Claude's slash popup can swallow one selecting
# the completion, and the next one submits.
_SUBMIT_TRIES = 3
_SUBMIT_WAIT = 2.0


def _open_panel(pane: str, command: str) -> bool:
    """Type `command`, submit it, and wait for the settings panel it opens.

    Not tmux.send_text: its submit check takes the text after the last ❯ on
    screen for the composer, and a settings panel has no composer. What is left
    there is the command's own echo above the panel ("❯ /status"), whenever the
    panel is short enough to leave it on screen. send_text read that as a command
    still waiting for its Enter, pressed Enter into the open panel, and reported
    failure for a /status that had run. The panel opening is what says it ran.
    """
    if not tmux._send_until_landed(pane, command, _MARKER):
        return False
    time.sleep(tmux._SLASH_SETTLE)
    for _ in range(_SUBMIT_TRIES):
        tmux.send_keys(pane, "Enter")
        if actions._wait_pane(pane, _panel_open, _SUBMIT_WAIT, _POLL) is not None:
            return True
        # Press again only while the command still sits in the composer, an
        # Enter the popup swallowed. Otherwise the panel is slow to draw, and
        # another Enter would land in it. One capture for both questions: a
        # panel that opens in between leaves the echo, which reads as stranded.
        text = tmux.capture_pane(pane).get("text", "")
        if _panel_open(text):
            return True
        if not tmux._tail_in(text, command, _MARKER):
            return actions._wait_pane(pane, _panel_open, _PANEL_WAIT, _POLL) is not None
    return False


def _screen_tail(pane: str) -> str:
    """The last lines the pane shows, for an error to say where Claude stopped."""
    text = tmux.capture_pane(pane).get("text", "")
    return " · ".join(ln.strip() for ln in text.splitlines() if ln.strip())[-200:]


def _read_account(pane: str) -> dict:
    """The account /status names, best effort: {} when the tab never drew."""
    text = None
    if _open_panel(pane, "/status"):
        text = actions._wait_pane(pane, lambda t: "Version:" in t, _PANEL_WAIT, _POLL)
    # Whatever happened, /usage needs the composer back: typed into an open
    # panel, it never lands.
    actions._escape_until(pane, lambda t: not _panel_open(t), 3, 0.3)
    actions._wait_pane(pane, _at_composer, _PANEL_WAIT, _POLL)
    return parse_account(text or "")


def _read_limits(pane: str) -> dict:
    if not _open_panel(pane, "/usage"):
        return {"ok": False, "error": f"/usage did not open its panel: {_screen_tail(pane)}"}
    text = actions._wait_pane(pane, usage_settled, _USAGE_WAIT, _POLL)
    if text is None:
        text = tmux.capture_pane(pane).get("text", "")
    limits = parse_limits(text)
    if not limits:
        said = _panel_says(text)
        return {"ok": False, "error": "no limits on Claude's /usage panel"
                + (f": {said}" if said else "")}
    # Still "Refreshing…" when the wait ran out: these are the numbers Claude
    # had cached, which is still what it would show a person.
    return {"ok": True, "limits": limits, "refreshing": _REFRESHING in text}


def _probe() -> dict:
    try:
        PROBE_CWD.mkdir(exist_ok=True)
    except OSError as e:
        return {"ok": False, "error": f"cannot make {PROBE_CWD}: {e}"}
    with tmux.private_server(f"claude-board-usage-{os.getpid()}"):
        r = tmux.new_window(str(PROBE_CWD), ["claude", "--model", "haiku"])
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error") or "could not start claude"}
        pane = r["pane_id"]
        tmux.resize_window(pane, _COLS, _ROWS)
        # Where the board's directory is trusted, PROBE_CWD is too; elsewhere
        # this is the first claude to open it.
        actions.confirm_trust_prompt(pane)
        if actions._wait_pane(pane, _at_composer, _START_WAIT, _POLL) is None:
            return {"ok": False,
                    "error": f"claude never reached its prompt: {_screen_tail(pane)}"}
        account = _read_account(pane)
        return {**_read_limits(pane), "account": account}


def read_usage() -> dict:
    """{ok, account: {login, email}, limits: [{label, used, resets}], refreshing,
    at} for this machine's Claude login, or {ok: False, error, at}.

    One probe at a time. A request that waited out another's probe takes that
    probe's answer: it was read after the request came in, so it is as fresh as
    a probe of its own would have been, and two open pages clicking at once
    start one claude, not two."""
    global _last
    asked = time.time()
    with _lock:
        if _last is not None and _last["at"] >= asked:
            return _last
        _last = {**_probe(), "at": time.time()}
        return _last

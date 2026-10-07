"""This machine's plan usage: Claude's off /status and /usage, Codex's off its
app-server.

How much of the current session and of the week is gone, and when each resets,
is drawn by Claude Code's /usage panel and kept nowhere on disk. So the board
asks Claude the way a person would: it starts a throwaway `claude` on a tmux
server of its own, reads the account off /status and the limits off /usage, and
kills the server. Neither command reaches the model. The session runs on haiku
anyway, so a keystroke that went astray costs a haiku turn and nothing more.

The probe runs in sessions.PROBE_CWD, which discovery never cards, and on a
server no user attaches to, so nothing about it shows on the board or in tmux.

Codex has no panel to read and needs none: `codex app-server` answers
account/read and account/rateLimits/read over stdio JSON-RPC, fresh from the
server and without a turn, and discovery never cards an app-server.

An account is logged in per machine, so each board answers for its own host; a
peer's usage is read by the peer's board (app.py forwards the request).
"""
from __future__ import annotations

import datetime
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from typing import Callable

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

# One probe at a time per CLI, and the answer each last gave (read_usage).
_locks = {"claude": threading.Lock(), "codex": threading.Lock()}
_last: dict[str, dict] = {}


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


# ── Codex ────────────────────────────────────────────────────────────────────

# A cold app-server answers both in about two seconds; the limits are a round
# trip to OpenAI's server.
_CODEX_WAIT = 20.0

_CODEX_REQUESTS = (
    {"id": 1, "method": "initialize",
     "params": {"clientInfo": {"name": "claude-board", "version": "0"}}},
    {"method": "initialized"},
    {"id": 2, "method": "account/read", "params": {}},
    {"id": 3, "method": "account/rateLimits/read"},
)
_ACCOUNT_ID, _LIMITS_ID = 2, 3


def _cli_env(exe: str) -> dict:
    """_spawn_env with every directory on `exe`'s symlink chain on PATH.

    An npm install is a link into nvm's bin (~/.local/bin/codex → …/node/vX/bin/
    codex → …/codex.js) whose script starts `#!/usr/bin/env node`, and `node`
    lives in that middle hop's directory: a board whose PATH lacks it starts a
    codex that dies at once on "env: 'node': No such file or directory"."""
    env = tmux._spawn_env()
    path = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    hop = exe
    for _ in range(10):
        d = os.path.dirname(hop)
        if d and d not in path:
            path.append(d)
        if not os.path.islink(hop):
            break
        hop = os.path.join(d, os.readlink(hop))
    env["PATH"] = os.pathsep.join(path)
    return env


def _codex_rpc(exe: str) -> tuple[dict[int, dict], str]:
    """Ask `codex app-server` for the account and its limits: ({id: reply}, the
    tail of its stderr). A reply that never came is missing from the dict.

    Its stdin stays open until both replies are in: an app-server whose stdin
    closes exits there, before answering what it had already been sent. It runs
    in a process group of its own, killed whole once they are: an npm install's
    `codex` is node running the native binary as its child, which would outlive
    node and hold the pipe open."""
    want = {_ACCOUNT_ID, _LIMITS_ID}
    replies: dict[int, dict] = {}
    with tempfile.TemporaryFile("w+") as err:
        proc = subprocess.Popen([exe, "app-server"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=err, text=True,
                                env=_cli_env(exe), start_new_session=True)
        answered = threading.Event()

        def read() -> None:
            for line in proc.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("id") in want:
                    replies[msg["id"]] = msg
                    if want <= replies.keys():
                        break
            answered.set()  # every reply in, or the app-server is gone

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            proc.stdin.write("".join(json.dumps(r) + "\n" for r in _CODEX_REQUESTS))
            proc.stdin.flush()
            answered.wait(_CODEX_WAIT)
        except OSError:
            pass  # it died before reading them; its stderr says why
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.wait()
            reader.join(2)
            for pipe in (proc.stdin, proc.stdout):
                try:
                    pipe.close()
                except OSError:
                    pass  # stdin's unflushed requests, to a reader that is gone
        err.seek(0)
        tail = " · ".join(ln.strip() for ln in err.read().splitlines() if ln.strip())
    return dict(replies), tail[-200:]


def _window_label(mins) -> str:
    """A limit's name from its window, as Codex's /status names them."""
    if not isinstance(mins, int) or mins <= 0:
        return "Limit"
    if mins == 7 * 24 * 60:
        return "Weekly limit"
    if mins % (24 * 60) == 0:
        return f"{mins // (24 * 60)}d limit"
    if mins % 60 == 0:
        return f"{mins // 60}h limit"
    return f"{mins}m limit"


def _resets_text(at) -> str:
    """An epoch as this machine's local time, worded the way Claude's panel
    words its own: "Oct 13, 7:49pm (PDT)"."""
    if not isinstance(at, (int, float)) or at <= 0:
        return ""
    t = datetime.datetime.fromtimestamp(at).astimezone()
    hour = t.strftime("%I").lstrip("0")
    minute = f":{t:%M}" if t.minute else ""
    return f"{t:%b} {t.day}, {hour}{minute}{t.strftime('%p').lower()} ({t:%Z})"


def parse_codex_limits(result: dict) -> list[dict]:
    """[{label, used, resets}] off account/rateLimits/read's result: the plan's
    own limits first, then any a model has of its own, each window a row."""
    snaps = result.get("rateLimitsByLimitId") or {}
    if not snaps and result.get("rateLimits"):
        snaps = {"codex": result["rateLimits"]}
    out = []
    for limit_id, snap in sorted(snaps.items(), key=lambda kv: kv[0] != "codex"):
        if not isinstance(snap, dict):
            continue
        name = "" if limit_id == "codex" else (snap.get("limitName") or limit_id)
        for which in ("primary", "secondary"):
            w = snap.get(which)
            if not isinstance(w, dict) or not isinstance(w.get("usedPercent"), (int, float)):
                continue
            used = float(w["usedPercent"])
            label = _window_label(w.get("windowDurationMins"))
            out.append({"label": f"{name} · {label}" if name else label,
                        "used": int(used) if used.is_integer() else used,
                        "resets": _resets_text(w.get("resetsAt"))})
    return out


def parse_codex_account(result: dict) -> dict:
    """{login, email} off account/read's result, as Claude's /status words them."""
    acct = result.get("account") or {}
    kind = acct.get("type")
    if kind == "chatgpt":
        login = " ".join(filter(None, ["ChatGPT", acct.get("planType")]))
    elif kind == "apiKey":
        login = "API key"
    else:
        login = kind or ""
    return {"login": login, "email": acct.get("email") or ""}


def _rpc_error(reply: dict) -> str:
    e = reply.get("error")
    return (e.get("message") if isinstance(e, dict) else str(e or "")) or "error"


def _probe_codex() -> dict:
    exe = tmux._resolve_cli("codex")
    if exe is None:
        return {"ok": False, "missing": True, "error": "codex is not installed here"}
    try:
        replies, stderr = _codex_rpc(exe)
    except OSError as e:
        return {"ok": False, "error": f"could not start codex app-server: {e}"}
    account = parse_codex_account(replies.get(_ACCOUNT_ID, {}).get("result") or {})
    reply = replies.get(_LIMITS_ID)
    if reply is None:
        return {"ok": False, "account": account,
                "error": "codex app-server never answered" + (f": {stderr}" if stderr else "")}
    if "result" not in reply:
        return {"ok": False, "account": account, "error": f"codex: {_rpc_error(reply)}"}
    limits = parse_codex_limits(reply["result"] or {})
    if not limits:
        return {"ok": False, "account": account, "error": "codex reported no limits"}
    return {"ok": True, "account": account, "limits": limits, "refreshing": False}


# Looked up when called, so a test that patches a probe gets its patch.
_PROBES: dict[str, Callable[[], dict]] = {"claude": lambda: _probe(), "codex": lambda: _probe_codex()}


def read_usage(cli: str = "claude") -> dict:
    """{ok, cli, account: {login, email}, limits: [{label, used, resets}],
    refreshing, at} for this machine's `cli` login ("claude" or "codex"), or
    {ok: False, cli, error, at} — with `missing` when codex is not installed
    here. `cli` says which was read: a board from before Codex was read here
    ignores the field it was asked with and answers for Claude.

    One probe at a time per CLI. A request that waited out another's probe takes
    that probe's answer: it was read after the request came in, so it is as
    fresh as a probe of its own would have been, and two open pages clicking at
    once start one claude, not two."""
    if cli not in _PROBES:
        return {"ok": False, "cli": cli, "error": f"no usage to read for {cli!r}",
                "at": time.time()}
    asked = time.time()
    with _locks[cli]:
        last = _last.get(cli)
        if last is not None and last["at"] >= asked:
            return last
        _last[cli] = {**_PROBES[cli](), "cli": cli, "at": time.time()}
        return _last[cli]

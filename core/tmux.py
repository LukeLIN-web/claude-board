"""All tmux subprocess interaction lives here (Linux backend for spawn + inject).

Every tmux call goes through `_run`, which returns a structured dict and never
raises out to its caller. Higher layers (actions, routes) rely on that contract.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Optional

_TIMEOUT = 10
# Availability is probed at most once per this many seconds so the 2s dashboard
# poll never spawns a tmux subprocess on every tick.
_AVAILABLE_TTL = 5.0
_available_cache: dict = {}


def _clear_caches() -> None:
    """Reset memoized state (used by tests and on explicit refresh)."""
    _available_cache.clear()


# When the board server is itself launched from inside a Claude session, its
# environment carries CLAUDECODE / CLAUDE_CODE_* markers. tmux — and every claude
# we spawn through it — would inherit those and start as a *child session*, which
# does not persist a normal per-project transcript. With no transcript the card's
# timeline reads empty. Strip these so spawned sessions are always top-level.
_CHILD_ENV_KEYS = (
    "CLAUDECODE",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
)


def _venv_bin_dirs() -> set[str]:
    """The board's own virtualenv `bin` dir(s), or empty if not in a venv.

    `sys.prefix` diverges from `sys.base_prefix` exactly when the interpreter is
    running inside a venv, so it identifies the venv even when the server was
    started via `.venv/bin/uvicorn` without `activate` setting VIRTUAL_ENV.
    """
    dirs: set[str] = set()
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        dirs.add(os.path.join(sys.prefix, "bin"))
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        dirs.add(os.path.join(venv, "bin"))
    return {os.path.normpath(d) for d in dirs}


def _spawn_env() -> dict:
    """Process env for spawned sessions, with two kinds of board context removed.

    1. Claude child-session markers (see above) so sessions are top-level.
    2. The board's own virtualenv. run.sh activates `.venv`, which prepends
       `.venv/bin` to PATH and sets VIRTUAL_ENV. Spawned `claude` sessions work
       in arbitrary project directories and must see a clean interpreter —
       otherwise `which python` inside them resolves to the board's venv instead
       of whatever the project expects.
    """
    env = dict(os.environ)
    for k in _CHILD_ENV_KEYS:
        env.pop(k, None)
    bin_dirs = _venv_bin_dirs()
    if bin_dirs:
        env.pop("VIRTUAL_ENV", None)
        env.pop("PYTHONHOME", None)
        path = env.get("PATH", "")
        if path:
            kept = [p for p in path.split(os.pathsep)
                    if p and os.path.normpath(p) not in bin_dirs]
            env["PATH"] = os.pathsep.join(kept)
    return env


def _run(*args: str) -> dict:
    """Run `tmux <args>` and return {ok, rc, stdout, stderr, error}; never raise."""
    try:
        cp = subprocess.run(
            ["tmux", *args],
            capture_output=True, text=True, timeout=_TIMEOUT,
            env=_spawn_env(),
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return {"ok": False, "rc": None, "stdout": "", "stderr": "", "error": "tmux not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "rc": None, "stdout": "", "stderr": "", "error": f"tmux timed out after {_TIMEOUT}s"}
    except Exception as e:  # pragma: no cover - defensive; contract is never-raise
        return {"ok": False, "rc": None, "stdout": "", "stderr": "", "error": str(e)}
    ok = cp.returncode == 0
    return {
        "ok": ok,
        "rc": cp.returncode,
        "stdout": cp.stdout,
        "stderr": cp.stderr,
        "error": "" if ok else (cp.stderr.strip() or f"tmux exited {cp.returncode}"),
    }


def available() -> bool:
    """True if tmux is usable. Cached briefly to avoid per-poll subprocesses.

    Probes with `start-server` rather than `list-sessions`: the latter exits
    non-zero when there are zero sessions, which wrongly hid the spawn UI and
    made it impossible to create the first session from the dashboard. Starting
    the server succeeds with zero sessions and is the actual precondition for
    spawning, and is idempotent if a server is already running.
    """
    if os.environ.get("TMUX"):
        return True
    now = time.monotonic()
    cached = _available_cache.get("value")
    ts = _available_cache.get("ts", 0.0)
    if cached is not None and (now - ts) < _AVAILABLE_TTL:
        return cached
    value = _run("start-server")["ok"]
    _available_cache["value"] = value
    _available_cache["ts"] = now
    return value


def _norm_tty(tty: str) -> str:
    t = (tty or "").strip()
    if t.startswith("/dev/"):
        t = t[len("/dev/"):]
    return t


def list_panes() -> list[dict]:
    """All panes across all sessions as dicts: pane_id, tty, session, path."""
    fmt = "#{pane_id}\t#{pane_tty}\t#{session_name}\t#{pane_current_path}"
    r = _run("list-panes", "-a", "-F", fmt)
    if not r["ok"]:
        return []
    panes: list[dict] = []
    for line in r["stdout"].splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        pane_id, tty, session, path = parts[0], parts[1], parts[2], parts[3]
        panes.append({"pane_id": pane_id, "tty": tty, "session": session, "path": path})
    return panes


def pane_for_tty(tty: str) -> Optional[str]:
    """Resolve a pane id from a session's tty, or None (never a wrong match)."""
    target = _norm_tty(tty)
    if not target:
        return None
    for pane in list_panes():
        if _norm_tty(pane["tty"]) == target:
            return pane["pane_id"]
    return None


def pane_target(pane: str) -> Optional[str]:
    """Human-addressable target ("session:window.pane") for a pane id, or None."""
    if not pane:
        return None
    r = _run("display-message", "-p", "-t", pane,
             "#{session_name}:#{window_index}.#{pane_index}")
    if not r["ok"]:
        return None
    return r["stdout"].strip() or None


def _session_names() -> list[str]:
    r = _run("list-sessions", "-F", "#{session_name}")
    if not r["ok"]:
        return []
    return [ln.strip() for ln in r["stdout"].splitlines() if ln.strip()]


_DEFAULT_SESSION = "fleet"


def _resolve_target() -> dict:
    """Resolve the tmux session to host fleet windows.

    Returns {target, exists}. `exists=False` means the chosen session name does
    not exist yet and the caller must create it — this is the cold-start case
    (zero sessions) or a pinned `$FLEET_TMUX_SESSION` that hasn't been made yet.
    A `new-window` needs a host session to attach to; spawning/resuming from a
    fleet with no live sessions must create that host rather than dead-end.
    """
    sessions = _session_names()
    env_target = os.environ.get("FLEET_TMUX_SESSION")
    if env_target:
        return {"target": env_target, "exists": env_target in sessions}
    if sessions:
        return {"target": sessions[0], "exists": True}
    return {"target": _DEFAULT_SESSION, "exists": False}


def new_window(cwd: str, cmd: Optional[list[str]] = None) -> dict:
    """Open a new tmux window in `cwd` running `cmd`; returns {ok, pane_id, error?}.

    Defaults to spawning `claude --dangerously-skip-permissions` so the fleet can
    drive new sessions non-interactively (no per-action permission prompts blocking
    the pane). Callers that need a different command — e.g. forking or resuming an
    existing session — pass `cmd` explicitly.

    When no host session exists yet, `cmd` is launched as a fresh detached
    session (running directly, with no placeholder shell) so spawn/resume/fork
    work from a cold start instead of failing on an empty tmux server.
    """
    cmd = cmd or ["claude", "--dangerously-skip-permissions"]
    target = _resolve_target()
    if target["exists"]:
        r = _run("new-window", "-P", "-F", "#{pane_id}",
                 "-t", target["target"], "-c", cwd, *cmd)
    else:
        # Cold start (no tmux server yet). Don't let `new-session` be what forks
        # the server: on some tmux builds that daemon — and the long-lived pane
        # process under it — inherits the stdout/stderr pipe `_run`'s
        # subprocess.run is reading, so the read blocks waiting for an EOF that
        # never comes (the dashboard's "Spawning…" hang on a host with no tmux
        # server). Starting the server in its own call lets it daemonize and
        # close those fds first; the subsequent new-session then only attaches.
        _run("start-server")
        r = _run("new-session", "-d", "-s", target["target"],
                 "-P", "-F", "#{pane_id}", "-c", cwd, *cmd)
    if not r["ok"]:
        return {"ok": False, "error": r["error"]}
    return {"ok": True, "pane_id": r["stdout"].strip()}


def capture_pane(pane: str, scrollback: int = 0) -> dict:
    """Return the text of `pane` as {ok, text} (never raises).

    `scrollback` > 0 includes that many lines of history above the visible area —
    needed for tall interactive menus whose top options scroll off-screen.
    """
    args = ["capture-pane", "-p", "-t", pane]
    if scrollback > 0:
        args = ["capture-pane", "-p", "-S", f"-{scrollback}", "-t", pane]
    r = _run(*args)
    if not r["ok"]:
        return {"ok": False, "error": r["error"], "text": ""}
    return {"ok": True, "text": r["stdout"]}


def send_keys(pane: str, *keys: str) -> dict:
    """Send tmux key names (e.g. "1", "Enter", "Escape") into `pane`.

    Unlike send_text, these are interpreted as keys, not literal characters, so
    they drive interactive menus such as Claude's permission prompt.
    """
    if not keys:
        return {"ok": False, "error": "no keys"}
    r = _run("send-keys", "-t", pane, *keys)
    if not r["ok"]:
        return {"ok": False, "error": r["error"]}
    return {"ok": True}


# A leading "/" opens Claude Code's slash-command autocomplete popup. An Enter
# that arrives in the same instant as the pasted text races that popup and gets
# consumed selecting a completion instead of submitting, so the prompt is lost.
# Waiting this long before Enter lets the popup settle on the typed text.
_SLASH_SETTLE = 0.5

# Codex's TUI composer batches a fast literal-text burst with an Enter that
# lands in the same instant and swallows the Enter — the text stays in the
# composer unsubmitted. A short settle splits the burst from the Enter so it
# registers as a real submit. Applies to EVERY Codex prompt (not just slash),
# so callers pass it explicitly via send_text(settle_before_enter=...).
_CODEX_ENTER_SETTLE = 0.4


def send_text(pane: str, text: str, settle_before_enter: float = 0.0) -> dict:
    """Send `text` literally into `pane`, then a separate Enter to submit it.

    `settle_before_enter` pauses between the pasted text and the Enter. Some TUIs
    (Codex always; Claude when a slash-command popup is open) coalesce a rapid
    text burst with an immediately-following Enter and drop the Enter instead of
    submitting; the settle lets the composer catch up. The slash case is detected
    here; platform-wide needs (e.g. Codex) are passed in by the caller. When both
    apply, the longer wait wins.
    """
    literal = _run("send-keys", "-t", pane, "-l", "--", text)
    if not literal["ok"]:
        return {"ok": False, "error": literal["error"]}
    delay = settle_before_enter
    if text.lstrip().startswith("/"):
        delay = max(delay, _SLASH_SETTLE)
    if delay > 0:
        time.sleep(delay)
    enter = _run("send-keys", "-t", pane, "Enter")
    if not enter["ok"]:
        return {"ok": False, "error": enter["error"]}
    return {"ok": True}

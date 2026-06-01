"""All tmux subprocess interaction lives here (Linux backend for spawn + inject).

Every tmux call goes through `_run`, which returns a structured dict and never
raises out to its caller. Higher layers (actions, routes) rely on that contract.
"""
from __future__ import annotations

import os
import subprocess
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


def _run(*args: str) -> dict:
    """Run `tmux <args>` and return {ok, rc, stdout, stderr, error}; never raise."""
    try:
        cp = subprocess.run(
            ["tmux", *args],
            capture_output=True, text=True, timeout=_TIMEOUT,
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
    """True if tmux is usable. Cached briefly to avoid per-poll subprocesses."""
    if os.environ.get("TMUX"):
        return True
    now = time.monotonic()
    cached = _available_cache.get("value")
    ts = _available_cache.get("ts", 0.0)
    if cached is not None and (now - ts) < _AVAILABLE_TTL:
        return cached
    value = _run("list-sessions")["ok"]
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


def _session_names() -> list[str]:
    r = _run("list-sessions", "-F", "#{session_name}")
    if not r["ok"]:
        return []
    return [ln.strip() for ln in r["stdout"].splitlines() if ln.strip()]


def _resolve_target() -> dict:
    """Pick the tmux session to spawn into: $FLEET_TMUX_SESSION or the first one."""
    sessions = _session_names()
    env_target = os.environ.get("FLEET_TMUX_SESSION")
    if env_target:
        if env_target in sessions:
            return {"ok": True, "target": env_target}
        return {"ok": False, "error": f"FLEET_TMUX_SESSION '{env_target}' is not a current tmux session"}
    if sessions:
        return {"ok": True, "target": sessions[0]}
    return {"ok": False, "error": "no tmux session available to spawn into"}


def new_window(cwd: str) -> dict:
    """Open a new tmux window running `claude` in `cwd`; returns {ok, pane_id, error?}.

    Spawned sessions launch with `--dangerously-skip-permissions` so the fleet can
    drive them non-interactively (no per-action permission prompts blocking the pane).
    """
    target = _resolve_target()
    if not target["ok"]:
        return {"ok": False, "error": target["error"]}
    r = _run("new-window", "-P", "-F", "#{pane_id}",
             "-t", target["target"], "-c", cwd,
             "claude", "--dangerously-skip-permissions")
    if not r["ok"]:
        return {"ok": False, "error": r["error"]}
    return {"ok": True, "pane_id": r["stdout"].strip()}


def send_text(pane: str, text: str) -> dict:
    """Send `text` literally into `pane`, then a separate Enter to submit it."""
    literal = _run("send-keys", "-t", pane, "-l", "--", text)
    if not literal["ok"]:
        return {"ok": False, "error": literal["error"]}
    enter = _run("send-keys", "-t", pane, "Enter")
    if not enter["ok"]:
        return {"ok": False, "error": enter["error"]}
    return {"ok": True}

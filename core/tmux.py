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
        for v in _VENV_MARKER_VARS:
            env.pop(v, None)
        path = env.get("PATH", "")
        if path:
            kept = [p for p in path.split(os.pathsep)
                    if p and os.path.normpath(p) not in bin_dirs]
            env["PATH"] = os.pathsep.join(kept)
    return env


def _socket_args() -> list[str]:
    """`-L <name>` server selector, or empty for the default tmux server.

    `FLEET_TMUX_SOCKET=juyi` makes every call `tmux -L juyi …`, pinning the board
    to an isolated server (its own socket + server process) instead of the shared
    default one — so spawned cards never land next to unrelated sessions. `-L` is
    a server option and must precede the tmux command. The session within that
    server is still chosen by `_resolve_target`; the socket only picks the server.
    """
    name = (os.environ.get("FLEET_TMUX_SOCKET") or "").strip()
    return ["-L", name] if name else []


# Env vars that mark the board's own virtualenv. `_spawn_env()` drops them, but a
# `pop` is not an `unset`: a long-lived tmux server started while `.venv` was
# active still holds these in its own environment and re-injects them into every
# new pane it forks — so a spawned session can inherit VIRTUAL_ENV from the stale
# server even after `_spawn_env()` cleaned the board's PATH. Wrapping the pane
# command in `env -u …` force-unsets them in the spawned process itself, which is
# correct regardless of how old the tmux server's environment is.
_VENV_MARKER_VARS = ("VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "PYTHONHOME")


def _venv_unset_prefix() -> list[str]:
    """`env -u …` argv prefix that strips the board's venv markers from a pane
    command. Empty when the board isn't running inside a venv (nothing to strip)."""
    if not _venv_bin_dirs():
        return []
    prefix = ["env"]
    for v in _VENV_MARKER_VARS:
        prefix += ["-u", v]
    return prefix


def _run(*args: str) -> dict:
    """Run `tmux <args>` and return {ok, rc, stdout, stderr, error}; never raise."""
    try:
        cp = subprocess.run(
            ["tmux", *_socket_args(), *args],
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


def pane_current_command(pane: str) -> Optional[str]:
    """Name of the pane's foreground process (tmux #{pane_current_command}), or
    None when it can't be resolved. Best-effort — callers must fail open."""
    if not pane:
        return None
    r = _run("display-message", "-p", "-t", pane, "#{pane_current_command}")
    if not r["ok"]:
        return None
    return r["stdout"].strip() or None


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
    # Force-unset the board's venv markers on the pane command itself so a stale
    # tmux server can't re-inject VIRTUAL_ENV into the spawned session.
    cmd = [*_venv_unset_prefix(), *cmd]
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
#
# The catch-up time grows with paste size: a big multi-paragraph prompt is still
# being ingested/re-wrapped when a flat 0.4s Enter arrives, so it gets dropped
# and the prompt sits unsent. Scale the settle with length instead of trusting a
# single magic number, and back it with a submit-verify (see send_text) so the
# rare tail case still self-heals rather than silently stranding the prompt.
_CODEX_ENTER_SETTLE = 0.4          # base/floor
_CODEX_ENTER_SETTLE_PER_KCHAR = 0.5  # extra seconds per 1000 chars pasted
_CODEX_ENTER_SETTLE_MAX = 3.0


# After the submit Enter, re-check the composer this many times, waiting this
# long each round, resending Enter while our text is still stranded there.
_SUBMIT_VERIFY_RETRIES = 3
_SUBMIT_VERIFY_WAIT = 0.4

# Before the submit Enter, confirm the literal text actually reached the
# composer. A busy pane mid-re-render can drop the injected keystrokes outright,
# so a following Enter would submit an empty line and the prompt would vanish
# with no trace. One wait per re-send attempt, escalating: a churning TUI often
# takes well over 0.15s to echo a paste (a stalled one buffers it for seconds),
# and a fixed short wait misreports that lag as a dropped prompt.
_LANDED_VERIFY_WAITS = (0.15, 0.5, 1.2, 2.5)


def codex_enter_settle(text_len: int) -> float:
    """Length-scaled settle before Codex's submit Enter (see _CODEX_ENTER_SETTLE)."""
    scaled = _CODEX_ENTER_SETTLE + (text_len / 1000.0) * _CODEX_ENTER_SETTLE_PER_KCHAR
    return min(scaled, _CODEX_ENTER_SETTLE_MAX)


# Footer line of an open /btw aside overlay (same anchor actions._overlay_anchors
# keys on). While the aside is open, the composer line STILL shows the "/btw …"
# command and the overlay echoes it again below — and a resent Enter would
# dismiss the overlay, killing the aside mid-answer. So an on-screen footer
# means "submitted", never "stranded". Any pre-existing overlay was closed by
# actions.send_prompt before the send, so by verify time the footer can only
# belong to the aside this very submit opened.
_BTW_OVERLAY_FOOTER = "Esc to close"


def _composer_has_tail(pane: str, text: str, marker: str = "❯") -> bool:
    """True if a distinctive tail of `text` still sits in `pane`'s composer,
    stranded and awaiting a submit Enter.

    The composer is the region after the last prompt marker — `marker` is the
    DRIVEN platform's composer glyph (`❯` for Claude, `›` for Codex), and only
    that one is searched. The other TUI's glyph is ordinary content here: Claude
    renders `›` as the " › blocked by #N" task-list separator, which the todo
    summary draws BELOW the composer — searching for both markers would anchor
    on that trailing `›`, look past the real composer, and wrongly report a
    landed prompt as "never landed". A submitted prompt is echoed as a turn ABOVE
    the marker (same glyph, hence "last") and leaves the composer empty (a dim
    ghost suggestion, never our text). Whitespace is squeezed on both sides so
    the needle survives the composer's soft-wrapping and indentation.

    Exception: a /btw aside keeps its command text on the composer line for as
    long as its answer overlay is open, so the overlay footer in the region
    means the prompt DID submit (see _BTW_OVERLAY_FOOTER).

    No marker on screen means there is NO composer — the TUI exited or was
    suspended and its parent shell owns the pty. The injected text still echoes
    there (line-discipline echo at a bash prompt), and matching that echo would
    make send_text press Enter and EXECUTE the prompt as a shell command.
    """
    needle = "".join(text.split())[-24:]
    if not needle:
        return False
    cap = capture_pane(pane).get("text", "")
    idx = cap.rfind(marker)
    if idx == -1:
        return False
    region = cap[idx:]
    if _BTW_OVERLAY_FOOTER in region:
        return False
    return needle in "".join(region.split())


def _send_until_landed(pane: str, text: str, marker: str = "❯") -> bool:
    """Send `text` literally into `pane`, confirming it reached the composer.

    A busy pane can drop the injected keystrokes during a re-render, so the text
    never arrives and a following Enter submits nothing — the prompt vanishes
    with no transcript trace (the dashboard then shows a phantom "Queued"). Re-
    send until the composer holds our text, clearing any partial paste with
    Ctrl-U first so a retry can't concatenate into a corrupted prompt. Returns
    False if the text never lands after the retries — the caller then reports the
    failure rather than pressing Enter on a lost prompt.

    A fully stalled TUI (frozen spinner, event loop wedged) never drops the
    keystrokes at all — the pty buffers them and they land whenever the pane
    wakes, possibly minutes after we've given up. Giving up therefore ends with
    a cleanup Ctrl-U queued behind everything we sent: when the pane wakes it
    wipes the late-landing text, so "never landed" stays truthful and the
    stranded prompt can't concatenate into the next send.
    """
    for attempt, wait in enumerate(_LANDED_VERIFY_WAITS):
        if attempt:
            _run("send-keys", "-t", pane, "C-u")  # drop any partial before retry
        literal = _run("send-keys", "-t", pane, "-l", "--", text)
        if not literal["ok"]:
            return False
        time.sleep(wait)
        if _composer_has_tail(pane, text, marker):
            return True
    _run("send-keys", "-t", pane, "C-u")  # wipe the buffered text on wake
    return False


def send_text(
    pane: str,
    text: str,
    settle_before_enter: float = 0.0,
    verify_submit: bool = False,
    verify_landed: bool = False,
    marker: str = "❯",
) -> dict:
    """Send `text` literally into `pane`, then a separate Enter to submit it.

    `settle_before_enter` pauses between the pasted text and the Enter. Some TUIs
    (Codex always; Claude when a slash-command popup is open) coalesce a rapid
    text burst with an immediately-following Enter and drop the Enter instead of
    submitting; the settle lets the composer catch up. The slash case is detected
    here; platform-wide needs (e.g. Codex) are passed in by the caller. When both
    apply, the longer wait wins.

    `verify_landed` (Claude) confirms the literal text actually reached the
    composer before Enter, re-sending it if a busy-pane re-render dropped the
    keystrokes — otherwise the Enter submits an empty line and the prompt is
    lost. `verify_submit` (Codex) confirms the composer actually emptied after
    Enter and resends Enter a couple of times if the prompt is still sitting
    there, so an under-tuned settle can't silently strand a prompt. Slash prompts
    verify submit unconditionally: even after the settle, Claude's popup can
    consume the Enter selecting the highlighted completion, leaving the command
    (e.g. "/clear") in the composer unsubmitted — the resent Enter then submits
    it for real.
    """
    if verify_landed:
        if not _send_until_landed(pane, text, marker):
            return {"ok": False, "error": "prompt text never landed in composer"}
    else:
        literal = _run("send-keys", "-t", pane, "-l", "--", text)
        if not literal["ok"]:
            return {"ok": False, "error": literal["error"]}
    is_slash = text.lstrip().startswith("/")
    delay = settle_before_enter
    if is_slash:
        delay = max(delay, _SLASH_SETTLE)
    if delay > 0:
        time.sleep(delay)
    enter = _run("send-keys", "-t", pane, "Enter")
    if not enter["ok"]:
        return {"ok": False, "error": enter["error"]}
    if verify_submit or is_slash:
        for _ in range(_SUBMIT_VERIFY_RETRIES):
            time.sleep(_SUBMIT_VERIFY_WAIT)
            if not _composer_has_tail(pane, text, marker):
                break
            resent = _run("send-keys", "-t", pane, "Enter")
            if not resent["ok"]:
                return {"ok": False, "error": resent["error"]}
        else:
            if _composer_has_tail(pane, text, marker):
                return {"ok": False, "error": "prompt still unsent after retries"}
    return {"ok": True}

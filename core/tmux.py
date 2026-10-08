"""All tmux subprocess interaction lives here (Linux backend for spawn + inject).

Every tmux call goes through `_run`, which returns a structured dict and never
raises out to its caller. Higher layers (actions, routes) rely on that contract.
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from typing import Callable, Optional

_TIMEOUT = 10
# Availability is probed at most once per this many seconds so the 2s dashboard
# poll never spawns a tmux subprocess on every tick.
_AVAILABLE_TTL = 5.0
_available_cache: dict = {}

# pane_for_tty runs for every card on every poll (more for a card the snapshot
# looks at closely), and each lookup used to fork `list-panes -a`. A pane's tty
# never changes, so the tty→pane map is reused for this long; a miss always
# re-lists, so a pane spawned since the last listing is never reported missing.
_PANE_MAP_TTL = 1.0
_pane_map_cache: dict = {}


def _clear_caches() -> None:
    """Reset memoized state (used by tests and on explicit refresh)."""
    _available_cache.clear()
    _pane_map_cache.clear()


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

    `FLEET_TMUX_SOCKET=board` makes every call `tmux -L board …`, pinning the board
    to an isolated server (its own socket + server process) instead of the shared
    default one — so spawned cards never land next to unrelated sessions. `-L` is
    a server option and must precede the tmux command. The session within that
    server is still chosen by `_resolve_target`; the socket only picks the server.
    A thread inside `private_server` goes to that server instead.
    """
    name = getattr(_private, "server", None) or (os.environ.get("FLEET_TMUX_SOCKET") or "").strip()
    return ["-L", name] if name else []


# The server a thread's calls go to while it is inside `private_server`. Per
# thread, because the board answers requests on a thread pool: one request's
# private pane must not pull another request's send onto its server.
_private = threading.local()


@contextmanager
def private_server(name: str):
    """Send this thread's tmux calls to server `name` for the block, then kill it.

    For a pane the board opens for its own use: it sits in no session a user
    attaches to, and whatever runs in it goes with the server when the block
    exits, however it exits. tmux leaves the socket file behind on kill-server,
    so that goes too.
    """
    prev = getattr(_private, "server", None)
    _private.server = name
    try:
        yield
    finally:
        _run("kill-server")
        _private.server = prev
        sock = os.path.join(os.environ.get("TMUX_TMPDIR") or "/tmp", f"tmux-{os.getuid()}", name)
        try:
            os.unlink(sock)
        except OSError:
            pass


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


# Where a user-installed CLI lives when the board's own PATH cannot see it.
# `claude` and `codex` install into ~/.local/bin — an entry a login shell adds
# to PATH and a bare environment does not, so a board brought up by a
# supervisor restart, a systemd unit or a cron line never has it. `hmz` is a pip
# entry point, so it lands in the conda base's bin — added to PATH by `conda
# init` in .bashrc, which a bare environment never sources either.
_CLI_FALLBACK_DIRS = ("~/.local/bin", "/usr/local/bin",
                      "~/miniconda3/bin", "~/anaconda3/bin", "~/miniforge3/bin")


def _resolve_cli(name: str) -> Optional[str]:
    """Absolute path to the CLI `name`, or None when it is nowhere on disk.

    A spawned pane does NOT inherit the tmux *server's* PATH: tmux hands it the
    environment of the client that ran `new-window` — the board process. So a
    board whose own PATH lacks ~/.local/bin spawns `claude` into a pane that
    cannot find it, and tmux still creates the window and prints its pane id
    before the exec fails 127 and takes the window with it. That is the spawn
    that reports success and never becomes a card (dashboard toast "Spawned",
    no session anywhere), and it is why the launch must not depend on whichever
    PATH the board happened to be started with.
    """
    if not name:
        return None
    if os.path.isabs(name):
        return name if os.access(name, os.X_OK) else None
    found = shutil.which(name, path=_spawn_env().get("PATH"))
    if found:
        return found
    for d in _CLI_FALLBACK_DIRS:
        candidate = os.path.join(os.path.expanduser(d), name)
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def _pane_env_prefix(exe: str) -> list[str]:
    """`env …` argv prefix for the pane command that runs `exe`; empty if none.

    Two fixes ride on the pane command itself, in the order `env` takes them
    (`-u` options before assignments):

    1. the board's venv markers are force-unset (`_venv_unset_prefix`);
    2. `exe`'s own directory is appended to the pane's PATH when that PATH lacks
       it. Launching by absolute path gets `claude` running in a bare-PATH pane,
       but not what runs inside it: the session's hooks and Bash calls still
       could not find `jq`, `claude` or `codex` in ~/.local/bin, so a project's
       goal-monitor hook died on "jq not on PATH" in every board-spawned card.
       Appended, not prepended: it fills in what the pane could not find and
       never shadows what it already resolves (a conda-base `hmz` must not hand
       its pane conda's `python`).
    """
    prefix = _venv_unset_prefix()
    # The pane's PATH is the board's spawn PATH: tmux gives a new pane the
    # environment of the client that ran new-window (see _resolve_cli).
    path = _spawn_env().get("PATH", "")
    exe_dir = os.path.dirname(exe)
    on_path = {os.path.normpath(p) for p in path.split(os.pathsep) if p}
    if exe_dir and os.path.normpath(exe_dir) not in on_path:
        prefix = (prefix or ["env"]) + [
            f"PATH={path}{os.pathsep}{exe_dir}" if path else f"PATH={exe_dir}"]
    return prefix


def _run(*args: str, input: Optional[str] = None) -> dict:
    """Run `tmux <args>` and return {ok, rc, stdout, stderr, error}; never raise.
    `input` is fed to tmux's stdin (for `load-buffer -`); otherwise stdin is closed."""
    stdin = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    try:
        cp = subprocess.run(
            ["tmux", *_socket_args(), *args],
            capture_output=True, text=True, timeout=_TIMEOUT,
            env=_spawn_env(),
            **stdin,
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
    """All panes across all sessions as dicts: pane_id, tty."""
    r = _run("list-panes", "-a", "-F", "#{pane_id}\t#{pane_tty}")
    if not r["ok"]:
        return []
    panes: list[dict] = []
    for line in r["stdout"].splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        panes.append({"pane_id": parts[0], "tty": parts[1]})
    return panes


def pane_for_tty(tty: str) -> Optional[str]:
    """Resolve a pane id from a session's tty, or None (never a wrong match)."""
    target = _norm_tty(tty)
    if not target:
        return None
    listed_at, panes = _pane_map_cache.get("map", (float("-inf"), {}))
    if time.monotonic() - listed_at < _PANE_MAP_TTL and target in panes:
        return panes[target]
    now = time.monotonic()
    panes = {}
    for pane in list_panes():
        panes.setdefault(_norm_tty(pane["tty"]), pane["pane_id"])
    _pane_map_cache["map"] = (now, panes)
    return panes.get(target)


def capture_tty(tty: Optional[str], scrollback: int = 0) -> Optional[str]:
    """Text of the pane running on `tty` (see capture_pane), or None when there
    is no such pane or it can't be captured."""
    pane = pane_for_tty(tty)
    if pane is None:
        return None
    cap = capture_pane(pane, scrollback=scrollback)
    return cap["text"] if cap["ok"] else None


def _display(pane: str, fmt: str) -> Optional[str]:
    """`fmt` expanded by tmux for `pane` (display-message), or None when tmux
    can't answer or the expansion is empty."""
    r = _run("display-message", "-p", "-t", pane, fmt)
    return (r["stdout"].strip() or None) if r["ok"] else None


def pane_current_command(pane: str) -> Optional[str]:
    """Name of the pane's foreground process (tmux #{pane_current_command}), or
    None when it can't be resolved. Best-effort — callers must fail open."""
    return _display(pane, "#{pane_current_command}") if pane else None


def pane_alive(pane: str) -> bool:
    """Whether `pane` still exists on the server.

    tmux tears a pane down the moment its command exits, so this is also how a
    spawn that died on exec is told apart from one that is running. Asked of
    the pane *list* rather than of the pane: `display-message -p -t %dead`
    exits 0 with empty output on tmux 3.2 instead of failing, so probing the
    target directly reports every dead pane as a live one.
    """
    if not pane:
        return False
    return any(p["pane_id"] == pane for p in list_panes())


def exit_copy_mode(pane: str) -> None:
    """Kick `pane` out of copy-mode before injecting keystrokes.

    A pane slips into copy-mode from a mouse scroll or view-mode, and while
    there tmux interprets every injected key as a copy-mode command — send-keys
    text is silently eaten and never reaches the TUI's composer. Best-effort:
    a failed probe is treated as "not in mode" and nothing is sent.
    """
    if _display(pane, "#{pane_in_mode}") == "1":
        _run("send-keys", "-t", pane, "-X", "cancel")


def pane_target(pane: str) -> Optional[str]:
    """Human-addressable target ("session:window.pane") for a pane id, or None."""
    if not pane:
        return None
    return _display(pane, "#{session_name}:#{window_index}.#{pane_index}")


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


# A pane id printed by `new-window` says the window was created, not that
# anything is running in it: a command that dies on exec takes the window with
# it milliseconds later. Re-probe the pane before calling a spawn good. A live
# pane answers the first probe, and a dead one is only declared dead once every
# wait has passed — a server too busy to answer one probe must never be
# reported as a failed spawn.
_SPAWN_LANDED_WAITS = (0.15, 0.35, 0.5)


def _spawn_landed(pane_id: str) -> bool:
    """Whether a freshly spawned pane is still there a moment later."""
    for wait in _SPAWN_LANDED_WAITS:
        time.sleep(wait)
        if pane_alive(pane_id):
            return True
    return False


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
    # Launch by absolute path: the pane gets the board's PATH, not the server's
    # (see _resolve_cli). Refusing here beats spawning a window that dies 127.
    exe = _resolve_cli(cmd[0])
    if exe is None:
        # Name the host: a peer's spawn error shows up on the aggregating
        # board's page, where "the board" would read as the wrong machine.
        return {"ok": False,
                "error": f"{cmd[0]} not found on {socket.gethostname()}: not on "
                         f"its board's PATH or in {', '.join(_CLI_FALLBACK_DIRS)} "
                         f"— install it there, or restart that board from a "
                         f"shell that can run {cmd[0]}"}
    # Force-unset the board's venv markers on the pane command itself so a stale
    # tmux server can't re-inject VIRTUAL_ENV into the spawned session, and give
    # the pane the directory `exe` was found in if its PATH lacks it.
    cmd = [*_pane_env_prefix(exe), exe, *cmd[1:]]
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
        # A new server numbers its panes from %0 again, so a tty→pane map read
        # off the old one could name a live pane of the new one — drop it.
        _pane_map_cache.clear()
        _run("start-server")
        r = _run("new-session", "-d", "-s", target["target"],
                 "-P", "-F", "#{pane_id}", "-c", cwd, *cmd)
    if not r["ok"]:
        return {"ok": False, "error": r["error"]}
    pane_id = r["stdout"].strip()
    if not pane_id:
        return {"ok": False, "error": "tmux opened the window but reported no pane id"}
    if not _spawn_landed(pane_id):
        return {"ok": False, "pane_id": pane_id,
                "error": f"the spawned pane exited immediately: {' '.join(cmd)}"}
    # A tmux server whose config has an error shows that error in the first pane
    # it opens, in view-mode, and view-mode eats every key sent to the pane: the
    # trust prompt's Down, the resume picker's, the first prompt's text. The cold
    # start above is exactly that first pane, so a board whose server had exited
    # (its own socket, after the last card closed) spawned a card it could not
    # drive. Leave the mode now, before anyone types into it.
    exit_copy_mode(pane_id)
    return {"ok": True, "pane_id": pane_id}


def resize_window(pane: str, cols: int, rows: int) -> dict:
    """Size the window holding `pane`. A detached session's window is 80x24, and
    nothing attached will ever widen it."""
    r = _run("resize-window", "-t", pane, "-x", str(cols), "-y", str(rows))
    return {"ok": True} if r["ok"] else {"ok": False, "error": r["error"]}


def capture_pane(pane: str, scrollback: int = 0) -> dict:
    """Return the text of `pane` as {ok, text} (never raises).

    `scrollback` > 0 includes that many lines of history above the visible area —
    needed for tall interactive menus whose top options scroll off-screen.
    """
    history = ("-S", f"-{scrollback}") if scrollback > 0 else ()
    r = _run("capture-pane", "-p", *history, "-t", pane)
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


# Footer line of an open /btw aside overlay (the same anchor
# btwscreen._overlay_anchors keys on). While the aside is open, the composer
# line STILL shows the "/btw …" command and the overlay echoes it again below —
# and a resent Enter would dismiss the overlay, killing the aside mid-answer.
# So an on-screen footer means "submitted", never "stranded". Any pre-existing
# overlay was closed by actions.send_prompt before the send, so by verify time
# the footer can only belong to the aside this very submit opened.
_BTW_OVERLAY_FOOTER = "Esc to close"

# Claude collapses a paste past ~1000 chars into a "[Pasted text #N]"
# placeholder (multi-line pastes render "[Pasted text #N +M lines]") and keeps
# the full content internally, expanding it on submit. The literal tail is
# never on screen, so requiring it made every long prompt fail landed-verify.
# Matched against the whitespace-squeezed composer region, hence no spaces.
_PASTED_PLACEHOLDER_RE = re.compile(r"\[Pastedtext#\d+[^\]]*\]")


def _needle(text: str) -> str:
    """The distinctive tail of `text` the landed/submitted checks look for,
    whitespace-squeezed so it survives the composer's soft-wrapping."""
    return "".join(text.split())[-24:]


# The glyph each CLI opens its composer line with, by Window.platform. hmz's
# composer is Claude's shape. Every read of a composer — has our text landed,
# is it empty, what is in it — anchors on the DRIVEN CLI's glyph and that one
# only, so every one of them takes it as an argument rather than guessing: the
# other CLI's glyph turns up as ordinary content. Claude draws `›` in its task
# list (" › blocked by #N"), which it puts BELOW the composer; a reader taking
# the last of either glyph as the composer read an empty Claude composer as
# holding "blocked by #N", cleared it blind on every landed-verify attempt, and
# blamed a failed send on "other text" that was never there.
COMPOSER_MARKERS = {"claude": "❯", "codex": "›", "hmz": "❯"}


def composer_marker(platform: str) -> str:
    """The composer glyph of `platform`'s CLI. Anything not named is driven
    down Claude's send path, so it is read with Claude's glyph too."""
    return COMPOSER_MARKERS.get(platform, COMPOSER_MARKERS["claude"])


def _composer_has_tail(pane: str, text: str, marker: str) -> bool:
    """_tail_in over a fresh capture of `pane`."""
    return _tail_in(capture_pane(pane).get("text", ""), text, marker)


def _tail_in(cap: str, text: str, marker: str) -> bool:
    """True if a distinctive tail of `text` still sits in the composer of the
    captured screen `cap`, stranded and awaiting a submit Enter.

    The composer is the region after the last `marker` (see COMPOSER_MARKERS:
    anchored on a Claude task line's `›` instead, the region starts below the
    real composer and a landed prompt reads as "never landed"). A submitted
    prompt is echoed as a turn ABOVE the marker (same glyph, hence "last") and
    leaves the composer empty (a dim ghost suggestion, never our text).
    Whitespace is squeezed on both sides so the needle survives the composer's
    soft-wrapping and indentation.

    Exception: a /btw aside keeps its command text on the composer line for as
    long as its answer overlay is open, so the overlay footer in the region
    means the prompt DID submit (see _BTW_OVERLAY_FOOTER).

    No marker on screen means there is NO composer — the TUI exited or was
    suspended and its parent shell owns the pty. The injected text still echoes
    there (line-discipline echo at a bash prompt), and matching that echo would
    make send_text press Enter and EXECUTE the prompt as a shell command.
    """
    needle = _needle(text)
    if not needle:
        return False
    idx = cap.rfind(marker)
    if idx == -1:
        return False
    region = cap[idx:]
    if _BTW_OVERLAY_FOOTER in region:
        return False
    squeezed = "".join(region.split())
    if _PASTED_PLACEHOLDER_RE.search(squeezed):
        return True
    return needle in squeezed


# One verified-clear pass reads the pane at most this many times; the blind
# fallback queues this many clearing presses. Sized to out-clear the worst
# wrapped paste a composer can hold: anything past ~1000 chars collapses to a
# one-line placeholder, and 1000 double-width CJK chars on a narrow (~60 col)
# pane wrap to ~35 lines.
_CLEAR_VERIFY_TRIES = 40
_CLEAR_BLIND_PRESSES = 40
_CLEAR_POLL = 0.1

# One clearing press. Ctrl-U alone is not enough (seen live on v2.1.286/287):
# it is readline's kill-to-start of the cursor's visual row, so text RIGHT of
# the cursor survives every press. With the cursor parked mid-line, a
# Ctrl-U-only clear left that tail in place, our text was typed in front of
# it, landed-verify still matched (it looks for our tail as a substring), and
# the prompt went out corrupted ("...DONELEFTOVER-tail"). End first takes the
# whole row. Ctrl-U on an emptied row deletes the newline above, but that reads
# as no progress on screen and sent multi-line leftovers to the blind fallback;
# Backspace joins the rows within the same press instead. On an empty composer
# all three keys are no-ops.
_CLEAR_KEYS = ("End", "C-u", "BSpace")

# Chrome the composer is boxed in: horizontal rules in the current borderless
# layout, box borders in older bordered builds.
_CHROME_CHARS = set("─│╭╮╰╯▔ ")


def _composer_text(cap_text: str, marker: str) -> Optional[str]:
    """What is sitting in the composer: the last `marker` line (after the
    marker) plus wrapped continuation lines, stopping at the chrome below it
    (rule / box border / blank / the ⏵⏵ status line). None when no marker is
    on screen — there is no composer to read. `marker` is the driven CLI's
    glyph alone (see COMPOSER_MARKERS)."""
    lines = cap_text.splitlines()
    last = None
    for i, ln in enumerate(lines):
        if marker in ln:
            last = i
    if last is None:
        return None
    head = lines[last]
    parts = [head[head.rfind(marker) + 1:].strip("│")]
    for ln in lines[last + 1:]:
        s = ln.strip()
        if not s or set(s) <= _CHROME_CHARS or s.startswith("⏵⏵"):
            break
        parts.append(ln.strip("│"))
    return "\n".join(p.strip() for p in parts).strip()


def _clear_composer(pane: str, marker: str) -> None:
    """Empty `pane`'s composer — the one `marker` opens — before a retry /
    after giving up on a send.

    Claude's composer removes at most one visual LINE per clearing press (see
    _CLEAR_KEYS), so a single press leaves most of a partial paste in place —
    the retry then concatenates into a corrupted prompt. Press per line,
    re-reading the pane until the composer is empty. When reading stops making
    progress (a stalled pane never redraws; ghost hint text never deletes) or
    the pane can't be captured, fall back to queueing blind presses: a stalled
    pty delivers them after the buffered text whenever it wakes, clearing it
    line by line, and every extra press is a no-op on an empty composer.
    """
    prev = None
    for _ in range(_CLEAR_VERIFY_TRIES):
        cap = capture_pane(pane)
        content = _composer_text(cap.get("text", ""), marker) if cap.get("ok") else None
        if content == "":
            return
        if content is None or content == prev:
            break  # unreadable or no progress — go blind
        prev = content
        send_keys(pane, *_CLEAR_KEYS)
        time.sleep(_CLEAR_POLL)
    for _ in range(_CLEAR_BLIND_PRESSES):
        send_keys(pane, *_CLEAR_KEYS)


# Post-mortem trace for the send path: landed-verify attempts append what the
# pane actually showed, so a "never landed" toast can be diagnosed from the
# recorded frames instead of re-probing a pane whose state is long gone.
# Host-suffixed for the same reason as uvicorn's log — the repo dir is on a
# shared mount and instances on other hosts would interleave a single file.
_SEND_DEBUG_LOG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    f"send_debug.{os.uname().nodename}.log")


def _send_debug(msg: str) -> None:
    """Best-effort append; a send must never fail because its trace can't."""
    # The test suite drives these same paths with fake panes; keep its noise
    # out of the log an operator reads to debug a real failed send.
    if "PYTEST_CURRENT_TEST" in os.environ:
        return
    try:
        with open(_SEND_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _literal_key_arg(text: str) -> str:
    """Escape a trailing semicolon for `send-keys -l`.

    tmux's command parser splits a command sequence on an argument that ENDS
    with an unescaped ";" — even when the argument arrives as its own argv
    element — so a prompt ending in ";" is typed minus its final character.
    The landed-verify's needle then keeps that ";" and can never match: every
    retry misses, the give-up path clears the composer, and the send reports
    "stayed empty" for a prompt that in fact landed. Mid-text semicolons are
    not separators; only the final one needs the escape."""
    return text[:-1] + "\\;" if text.endswith(";") else text


def _send_literal(pane: str, text: str) -> dict:
    """Type `text` into `pane` as literal characters, not key names: {ok[, error]}."""
    r = _run("send-keys", "-t", pane, "-l", "--", _literal_key_arg(text))
    return {"ok": True} if r["ok"] else {"ok": False, "error": r["error"]}


def _send_until_landed(pane: str, text: str, marker: str) -> bool:
    """Send `text` literally into `pane`, confirming it reached the composer.

    A busy pane can drop the injected keystrokes during a re-render, so the text
    never arrives and a following Enter submits nothing — the prompt vanishes
    with no transcript trace (the dashboard then shows a phantom "Queued"). Re-
    send until the composer holds our text, clearing the composer before EVERY
    attempt (see _clear_composer) so nothing already sitting there can
    concatenate into a corrupted prompt.
    Returns False if the text never lands after the retries — the caller then
    reports the failure rather than pressing Enter on a lost prompt.

    A fully stalled TUI (frozen spinner, event loop wedged) never drops the
    keystrokes at all — the pty buffers them and they land whenever the pane
    wakes, possibly minutes after we've given up. Giving up therefore ends with
    a cleanup clear queued behind everything we sent: when the pane wakes it
    wipes the late-landing text, so "never landed" stays truthful and the
    stranded prompt can't concatenate into the next send.
    """
    for attempt, wait in enumerate(_LANDED_VERIFY_WAITS):
        # Clear before every attempt, the FIRST one included. Whatever is in the
        # composer when we arrive takes the lead and our text lands appended to
        # it — and the landed check below matches a TAIL, so the concatenation
        # reads as a clean landing and the Enter submits the corrupted prompt.
        # The leftover is usually the board's own previous send: submit-verify
        # gives up with the text still sitting there, so pressing the card's
        # Clear on a wedged session typed "/clear", failed to submit it, and the
        # next Clear submitted the literal text "/clear/clear" — which Claude
        # answers as prose ("type /clear on its own line") instead of clearing,
        # leaving the session unclearable from the board for good.
        _clear_composer(pane, marker)
        literal = _send_literal(pane, text)
        if not literal["ok"]:
            _send_debug(f"landed pane={pane} attempt={attempt} "
                        f"send-keys FAILED: {literal.get('error')!r}")
            return False
        time.sleep(wait)
        # One capture, both judged and logged: a miss records the very frame it
        # was decided on, not a later one the pane has since redrawn.
        cap = capture_pane(pane).get("text", "")
        if _tail_in(cap, text, marker):
            return True
        idx = cap.rfind(marker)
        frame = ("".join(cap[idx:].split())[:200] if idx != -1
                 else f"NO-MARKER tail={cap[-120:]!r}")
        _send_debug(f"landed pane={pane} attempt={attempt} wait={wait} MISS "
                    f"needle={_needle(text)!r} frame={frame!r}")
    _clear_composer(pane, marker)  # wipe the buffered text on wake
    _send_debug(f"landed pane={pane} gave up after "
                f"{len(_LANDED_VERIFY_WAITS)} attempts")
    return False


def send_text(
    pane: str,
    text: str,
    settle_before_enter: float = 0.0,
    verify_landed: bool = False,
    *,
    marker: str,
) -> dict:
    """Send `text` literally into `pane`, then a separate Enter to submit it.

    `marker` is the composer glyph of the CLI in `pane` (composer_marker); every
    check below reads the composer it opens.

    `settle_before_enter` pauses between the pasted text and the Enter. Some TUIs
    (Codex always; Claude when a slash-command popup is open) coalesce a rapid
    text burst with an immediately-following Enter and drop the Enter instead of
    submitting; the settle lets the composer catch up. The slash case is detected
    here; platform-wide needs (e.g. Codex) are passed in by the caller. When both
    apply, the longer wait wins.

    `verify_landed` (Claude) confirms the literal text actually reached the
    composer before Enter, re-sending it if a busy-pane re-render dropped the
    keystrokes — otherwise the Enter submits an empty line and the prompt is
    lost. After Enter, the composer is checked to have actually emptied, and
    Enter resent a couple of times while the prompt is still sitting there, so an
    under-tuned settle (Codex) can't silently strand a prompt — and so can't
    Claude's slash popup, which even after the settle can consume the Enter
    selecting the highlighted completion, leaving the command (e.g. "/clear") in
    the composer unsubmitted; the resent Enter then submits it for real.
    """
    if verify_landed:
        if not _send_until_landed(pane, text, marker):
            return {"ok": False, "error": "prompt text never landed in composer",
                    "reason": "unlanded"}
    else:
        literal = _send_literal(pane, text)
        if not literal["ok"]:
            return literal
    delay = settle_before_enter
    if text.lstrip().startswith("/"):
        delay = max(delay, _SLASH_SETTLE)
    if delay > 0:
        time.sleep(delay)
    enter = send_keys(pane, "Enter")
    if not enter["ok"]:
        return enter
    for _ in range(_SUBMIT_VERIFY_RETRIES):
        time.sleep(_SUBMIT_VERIFY_WAIT)
        if not _composer_has_tail(pane, text, marker):
            break
        resent = send_keys(pane, "Enter")
        if not resent["ok"]:
            return resent
    else:
        if _composer_has_tail(pane, text, marker):
            return {"ok": False, "error": "prompt still unsent after retries"}
    return {"ok": True}


# A bracketed paste lands in hmz's composer in ~0.06s whatever its length. The
# length-scaled wait is for a TUI that hasn't turned bracketed paste on, where
# tmux delivers the paste as plain keystrokes and hmz re-renders per key (850
# chars of CJK took 1s that way, 1700 took 2.4s). Polled, so a quick landing
# costs nothing; nothing is resent meanwhile.
_CONFIRMED_LANDED_BASE = 3.0
_CONFIRMED_LANDED_PER_KCHAR = 3.0
_CONFIRMED_LANDED_MAX = 12.0
_CONFIRMED_POLL = 0.1
# After each Enter, how long to look for the TUI's own record of the line before
# deciding the Enter didn't take.
_CONFIRMED_TOOK_WAIT = 3.0
# Once taken, how long to watch for the TUI refusing it on screen (hmz answers
# a `$flow` it doesn't have from a worker, a moment after writing the line down).
_CONFIRMED_REFUSAL_WAIT = 1.5


def _shown_above_composer(pane: str, text: str, marker: str) -> bool:
    """True if the tail of `text` is on screen above an empty composer — the
    one `marker` opens: the line was submitted and echoed into the transcript."""
    needle = _needle(text)
    cap = capture_pane(pane).get("text", "")
    idx = cap.rfind(marker)
    if not needle or idx == -1 or _composer_text(cap, marker):
        return False
    return needle in "".join(cap[:idx].split())


def _paste(pane: str, text: str) -> dict:
    """Paste `text` into `pane` as one bracketed paste.

    Through a uniquely named buffer (buffers are server-wide, and two sends can
    overlap), loaded from stdin rather than argv, and deleted by the paste. -p
    brackets it when the app has asked for bracketed paste, so the app takes it
    as one Paste rather than a key per character; -r keeps a newline a newline
    instead of tmux's default CR, which an app without bracketed paste would
    read as Enter."""
    name = f"fleet-send-{os.getpid()}-{time.monotonic_ns()}"
    loaded = _run("load-buffer", "-b", name, "-", input=text)
    if not loaded["ok"]:
        return loaded
    pasted = _run("paste-buffer", "-p", "-r", "-d", "-b", name, "-t", pane)
    if not pasted["ok"]:
        _run("delete-buffer", "-b", name)
    return pasted


def send_text_confirmed(
    pane: str,
    text: str,
    took: Callable[[], bool],
    marker: str,
    refused: Optional[Callable[[], str]] = None,
) -> dict:
    """Paste `text` into `pane` once, Enter, and succeed only when `took()` says
    the TUI actually took the line — and, given `refused`, didn't then answer it
    with a refusal (what `refused()` returns) instead of acting on it.

    For hmz, where _send_until_landed + submit-verify lose prompts and report
    them sent (seen live: a 621-char prompt, ok returned, nothing ran). Typed a
    key at a time, a long prompt takes hmz seconds to ingest, and hmz resolves
    bound keys — Backspace, Ctrl-U, End — on its app pump AHEAD of the
    characters still queued at its editor. So the first 0.15s landed check
    missed, the clear-and-retype fired mid-ingest, the clear keys jumped the
    queue, the two copies and the clears interleaved, and the Enter sent an
    editor that ended up empty — which hmz ignores, and which an "is our tail
    gone from the composer" check reads as a submit.

    So the text goes in as one paste (see _paste), once, never resent, and is
    given as long as its length could need to land; and after Enter an emptied
    composer counts for nothing — only `took()`, the caller's positive evidence,
    does. Enter is resent only while the text still sits in the composer.
    """
    _clear_composer(pane, marker)
    pasted = _paste(pane, text)
    if not pasted["ok"]:
        return {"ok": False, "error": pasted["error"]}
    wait = min(_CONFIRMED_LANDED_BASE + len(text) / 1000.0 * _CONFIRMED_LANDED_PER_KCHAR,
               _CONFIRMED_LANDED_MAX)
    deadline = time.time() + wait
    while not _composer_has_tail(pane, text, marker):
        if time.time() >= deadline:
            _clear_composer(pane, marker)
            _send_debug(f"confirmed pane={pane} never landed in {wait:.1f}s")
            return {"ok": False, "error": "prompt text never landed in composer",
                    "reason": "unlanded"}
        time.sleep(_CONFIRMED_POLL)
    for _ in range(_SUBMIT_VERIFY_RETRIES):
        enter = send_keys(pane, "Enter")
        if not enter["ok"]:
            return enter
        until = time.time() + _CONFIRMED_TOOK_WAIT
        while time.time() < until:
            time.sleep(_CONFIRMED_POLL)
            if took():
                return _not_refused(refused)
        if not _composer_has_tail(pane, text, marker):
            break  # the composer let go of it and nothing took it; Enter won't help
    stranded = _composer_has_tail(pane, text, marker)
    _send_debug(f"confirmed pane={pane} not taken, stranded={stranded}")
    if stranded:
        return {"ok": False, "error": "prompt still unsent after retries"}
    return {"ok": False,
            "error": "the composer emptied but the prompt was never taken — not sent"}


def _not_refused(refused: Optional[Callable[[], str]]) -> dict:
    """The outcome of a line the TUI took: ok, unless it says no to it on screen.
    Taken and refused is no send: nothing runs, and the draft is kept."""
    if refused is None:
        return {"ok": True}
    until = time.time() + _CONFIRMED_REFUSAL_WAIT
    while True:
        said = refused()
        if said:
            _send_debug(f"confirmed taken, then refused: {said!r}")
            return {"ok": False, "error": f"taken but not run — {said}"}
        if time.time() >= until:
            return {"ok": True}
        time.sleep(_CONFIRMED_POLL)

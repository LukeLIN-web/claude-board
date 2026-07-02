"""Side-effectful actions: focus, fork, export, close, spawn, send-prompt."""
from __future__ import annotations

import os
import re
import signal
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from . import tmux
from .sessions import CLAUDE_HOME, find_window
from .transcripts import timeline, extract_plan_history, extract_skills_used, extract_memory_ops

# Upper bound on an injected single-line prompt (after newline collapse).
_MAX_PROMPT_CHARS = 8000

# Focus shim resolution: a user override at ~/.claude/focus-tty.sh wins; otherwise
# the bundled cross-setup default (Terminal.app / iTerm2 / tmux) shipped with the repo.
_USER_FOCUS_SCRIPT = CLAUDE_HOME / "focus-tty.sh"
_BUNDLED_FOCUS_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "focus-tty.sh"


def _resolve_focus_script() -> Optional[Path]:
    if _USER_FOCUS_SCRIPT.exists():
        return _USER_FOCUS_SCRIPT
    if _BUNDLED_FOCUS_SCRIPT.exists():
        return _BUNDLED_FOCUS_SCRIPT
    return None


def focus_terminal(tty: str) -> dict:
    """Activate the terminal tab that owns `tty`.

    Prefers a user override at ~/.claude/focus-tty.sh; falls back to the bundled
    scripts/focus-tty.sh, which handles plain Terminal.app / iTerm2 tabs and tmux
    panes on macOS out of the box.
    """
    if not tty:
        return {"ok": False, "error": "no tty"}
    script = _resolve_focus_script()
    if script is None:
        return {
            "ok": False,
            "error": f"no focus-tty.sh found (looked at {_USER_FOCUS_SCRIPT} and {_BUNDLED_FOCUS_SCRIPT})",
        }
    # Direct exec respects the script's own shebang (matches the original behavior
    # and any user override). If the +x bit was lost on an odd checkout, retry via
    # bash (covers bash/POSIX scripts; a non-bash override should keep its +x).
    # The whole thing is shielded so focus_terminal NEVER raises — a TimeoutExpired
    # (e.g. a blocking macOS Automation prompt) or a missing `bash` must return the
    # structured error, not bubble up as a 500 in the request handler.
    try:
        try:
            proc = subprocess.run(
                [str(script), tty],
                capture_output=True, text=True, timeout=10,
            )
        except PermissionError:
            proc = subprocess.run(
                ["bash", str(script), tty],
                capture_output=True, text=True, timeout=10,
            )
    except subprocess.TimeoutExpired:
        # stable contract: the child (e.g. a blocking Automation prompt) was killed
        return {"ok": False, "error": "focus timed out after 10s", "code": None}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    # `code` lets the UI distinguish the script's exit codes (3 detached / 4 no-tab
    # / 5 permission-denied / 6 unsupported) instead of a generic failure.
    return {
        "ok": proc.returncode == 0,
        "code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


_FORK_APPLESCRIPT_ITERM = '''
tell application "iTerm2"
    activate
    set newWin to (create window with default profile)
    tell current session of newWin
        write text {cmd}
    end tell
end tell
'''


def open_claude_window(cwd: str, claude_args: list[str]) -> dict:
    """Open `claude <claude_args>` in a new window, cwd-anchored.

    Prefers the tmux backend (Linux/headless); falls back to a new iTerm2 window
    via AppleScript on macOS. Returns a structured dict and never raises. If
    neither backend is available, returns a clear actionable error rather than
    the opaque `[Errno 2] No such file or directory: 'osascript'`.

    Resume/fork (the callers below) always launch with
    `--dangerously-skip-permissions`, matching fresh spawns (create_session ->
    tmux.new_window's default). The fleet drives these sessions unattended, so
    a per-action approval prompt would otherwise wedge a resumed /goal loop.
    """
    if "--dangerously-skip-permissions" not in claude_args:
        claude_args = ["--dangerously-skip-permissions", *claude_args]
    if tmux.available():
        r = tmux.new_window(cwd, ["claude", *claude_args])
        if r["ok"]:
            return {"ok": True, "cwd": cwd, "pane_id": r.get("pane_id"), "backend": "tmux"}
        return {"ok": False, "error": r["error"], "backend": "tmux"}

    if not shutil.which("osascript"):
        return {
            "ok": False,
            "error": "no terminal backend: start a tmux server (Linux) "
                     "or run on macOS with iTerm2 (osascript not found)",
        }

    args_str = " ".join(shlex.quote(a) for a in claude_args)
    inner = f"cd {shlex.quote(cwd)} && claude {args_str}"
    quoted_for_applescript = '"' + inner.replace('\\', '\\\\').replace('"', '\\"') + '"'
    script = _FORK_APPLESCRIPT_ITERM.format(cmd=quoted_for_applescript)
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": proc.returncode == 0,
        "cwd": cwd,
        "backend": "iterm",
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def open_codex_window(cwd: str, codex_args: list[str]) -> dict:
    """Open `codex <codex_args>` in a new window, cwd-anchored (tmux/iTerm2).

    Mirrors open_claude_window but launches the Codex CLI; used to fork/resume a
    Codex session into a fresh window.
    """
    if tmux.available():
        r = tmux.new_window(cwd, ["codex", *codex_args])
        if r["ok"]:
            return {"ok": True, "cwd": cwd, "pane_id": r.get("pane_id"), "backend": "tmux"}
        return {"ok": False, "error": r["error"], "backend": "tmux"}

    if not shutil.which("osascript"):
        return {
            "ok": False,
            "error": "no terminal backend: start a tmux server (Linux) "
                     "or run on macOS with iTerm2 (osascript not found)",
        }

    args_str = " ".join(shlex.quote(a) for a in codex_args)
    inner = f"cd {shlex.quote(cwd)} && codex {args_str}"
    quoted_for_applescript = '"' + inner.replace('\\', '\\\\').replace('"', '\\"') + '"'
    script = _FORK_APPLESCRIPT_ITERM.format(cmd=quoted_for_applescript)
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": proc.returncode == 0, "cwd": cwd, "backend": "iterm",
            "stdout": proc.stdout, "stderr": proc.stderr}


# Both lines are present only on Claude's "resume from summary?" picker, shown
# when resuming a large/old session. Requiring both avoids matching a session
# that merely printed the word "resume" in its output.
_RESUME_PICKER_MARKERS = ("Resume from summary", "Resume full session")


def _resume_picker_up(pane_id: str) -> bool:
    """True if `pane_id` currently shows Claude's resume-summary picker."""
    cap = tmux.capture_pane(pane_id)
    text = cap.get("text", "") if cap.get("ok") else ""
    return all(m in text for m in _RESUME_PICKER_MARKERS)


def confirm_resume_picker(
    pane_id: str,
    choice: str = "2",
    attempts: int = 15,
    interval: float = 0.4,
    settle: float = 0.6,
) -> dict:
    """Poll `pane_id` for Claude's resume-summary picker and auto-answer it.

    A freshly launched `claude --resume <id>` on a large/old session parks on a
    "Resume from summary / Resume full session" picker. The fleet drives resumed
    sessions unattended (on a headless host nobody is watching that detached
    pane), so we answer it. `choice` defaults to "2" — "Resume full session
    as-is".

    Driving a live TUI is unforgiving: a key sent the instant the menu paints is
    dropped (the menu text renders before its input handler is armed), and any
    key sent *after* the picker dismisses lands in the resumed session as stray
    text — which is how a misfire injects a `/compact` or a junk prompt into a
    real session. So this is deliberate: wait for the picker, settle, press the
    digit, then send Enter ONLY while the picker is still up, and verify it
    cleared. If the digit alone already confirmed (some builds do), the Enter is
    skipped so nothing leaks.

    Returns {confirmed, waited, reason}. `confirmed=False` with reason
    "no picker" means the session resumed straight to a live prompt (small
    session) — nothing to answer. Never raises.
    """
    if not pane_id:
        return {"confirmed": False, "waited": 0.0, "reason": "no pane"}
    waited = 0.0
    seen = False
    for _ in range(max(1, attempts)):
        if _resume_picker_up(pane_id):
            seen = True
            break
        time.sleep(interval)
        waited += interval
    if not seen:
        return {"confirmed": False, "waited": round(waited, 2), "reason": "no picker"}
    # Let the TUI's input handler arm before the first keypress.
    time.sleep(settle)
    tmux.send_keys(pane_id, choice)
    time.sleep(settle)
    # Confirm only if the digit selected without dismissing; skip Enter (avoid a
    # leak) if the picker is already gone.
    if _resume_picker_up(pane_id):
        tmux.send_keys(pane_id, "Enter")
        time.sleep(settle)
    gone = not _resume_picker_up(pane_id)
    return {
        "confirmed": gone,
        "waited": round(waited, 2),
        "reason": "" if gone else "picker still present",
    }


def fork_session(pid: int) -> dict:
    """Open a new window and fork the session (new ID, inherits history).

    Codex has no `--fork-session`; the closest is resuming the rollout into a
    fresh window, so codex sessions branch to `codex resume <session_id>`.
    """
    w = find_window(pid)
    if not w:
        return {"ok": False, "error": f"no window pid={pid}"}

    if getattr(w, "platform", "claude") == "codex":
        r = open_codex_window(w.cwd, ["resume", w.session_id])
    else:
        r = open_claude_window(w.cwd, ["--resume", w.session_id, "--fork-session"])
    r.setdefault("session_id", w.session_id)
    return r


def _render_session_markdown(pid: int, limit: int = 80) -> Optional[tuple[str, str]]:
    w = find_window(pid)
    if not w or not w.transcript_path:
        return None
    events = timeline(w.transcript_path, limit=limit)
    title = w.name or w.project_name or f"session-{w.session_id[:8]}"
    plan_hist = extract_plan_history(w.transcript_path)
    skills = extract_skills_used(w.transcript_path)
    mem_ops = extract_memory_ops(w.transcript_path)

    lines: list[str] = [
        f"# {title}",
        "",
        f"- project: `{w.cwd}`",
        f"- session: `{w.session_id}`",
        f"- pid: {w.pid} · status: {w.status} · version: {w.version}",
        f"- transcript: `{w.transcript_path}`",
    ]
    if skills:
        lines.append(f"- skills: {', '.join(skills)}")
    if mem_ops:
        ops_str = ", ".join(f"{'↓' if m['operation']=='read' else '↑'}{m['name']}" for m in mem_ops)
        lines.append(f"- memory: {ops_str}")
    lines.append("")

    if plan_hist:
        lines.append("## Plan 历史")
        lines.append("")
        for ph in plan_hist:
            ts = (ph.get("ts") or "")[:19]
            lines.append(f"### {ph['version_label']} — {ts} ({ph['plan_file']})")
            lines.append("")
            if ph["operation"] == "write" and ph.get("content"):
                lines.append("```")
                lines.append(ph["content"][:5000])
                lines.append("```")
            elif ph["operation"] == "edit" and ph.get("diff"):
                lines.append("```diff")
                lines.append(f"- {ph['diff']['old'][:1000]}")
                lines.append(f"+ {ph['diff']['new'][:1000]}")
                lines.append("```")
            lines.append("")

    lines.append("## 时间线")
    lines.append("")
    for ev in events:
        ts = (ev.get("ts") or "")[:19]
        kind = ev["kind"]
        if kind == "user_text":
            lines.append(f"### 👤 user `{ts}`")
            lines.append("")
            lines.append(ev["text"])
            lines.append("")
        elif kind == "assistant_text":
            lines.append(f"### 🤖 assistant `{ts}`")
            lines.append("")
            lines.append(ev["text"])
            lines.append("")
        elif kind == "tool_use":
            extras = ", ".join(f"{k}={v!r}" for k, v in ev.get("extra", {}).items())
            lines.append(f"- 🔧 `{ev['tool']}({extras})` `{ts}`")
        elif kind == "tool_result":
            snippet = (ev.get("text") or "").replace("\n", " ")[:120]
            lines.append(f"  - ↳ result: `{snippet}…`")
    return title, "\n".join(lines)


_EXPORT_MD = Path("/tmp/fleet-export.md")


def export_to_feishu(pid: int) -> dict:
    """Render session markdown and create a Feishu doc via lark-fnlp."""
    rendered = _render_session_markdown(pid)
    if not rendered:
        return {"ok": False, "error": "no session"}
    title, md = rendered

    _EXPORT_MD.write_text(md, encoding="utf-8")

    quoted_title = shlex.quote(title)
    cmd = (
        f"source ~/.zshrc 2>/dev/null; "
        f"cd /tmp && lark-fnlp docs +create "
        f"--title {quoted_title} "
        f"--markdown @./fleet-export.md "
        f"--as bot"
    )
    try:
        proc = subprocess.run(
            ["zsh", "-c", cmd],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        _EXPORT_MD.unlink(missing_ok=True)

    doc_url = None
    if proc.returncode == 0:
        import json as _json
        try:
            result = _json.loads(proc.stdout)
            doc_url = result.get("data", {}).get("doc_url")
        except Exception:
            pass

    return {
        "ok": proc.returncode == 0,
        "title": title,
        "doc_url": doc_url,
        "stdout": proc.stdout.strip()[-2000:],
        "stderr": proc.stderr.strip()[-2000:],
        "rc": proc.returncode,
    }


def close_session(pid: int) -> dict:
    """Gracefully terminate a Claude Code session by PID."""
    w = find_window(pid)
    if not w:
        return {"ok": False, "error": f"no window pid={pid}"}
    if not w.alive:
        return {"ok": True, "already_dead": True}
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return {"ok": True, "already_dead": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "pid": pid, "name": w.name or w.project_name}


def close_session(pid: int) -> dict:
    """Send SIGTERM to a Claude Code session for graceful shutdown."""
    import signal
    w = find_window(pid)
    if not w:
        return {"ok": False, "error": f"no window pid={pid}"}
    if not w.alive:
        return {"ok": True, "message": "already dead"}
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return {"ok": True, "message": "already dead"}
    except PermissionError:
        return {"ok": False, "error": "permission denied"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "pid": pid, "message": f"SIGTERM sent to {pid}"}


def create_session(cwd: str, platform: str = "claude") -> dict:
    """Spawn a new tmux window in `cwd` (validated server-side).

    `platform` selects the CLI: "claude" (default) launches Claude Code with
    permission prompts skipped; "codex" launches the Codex TUI in `--yolo` mode
    so the fleet can drive it without per-action approval prompts.
    """
    if not cwd or not cwd.strip():
        return {"ok": False, "error": "cwd is required"}
    resolved = os.path.expanduser(cwd.strip())
    if not os.path.isdir(resolved):
        return {"ok": False, "error": f"not a directory: {resolved}"}
    if platform == "codex":
        return tmux.new_window(resolved, ["codex", "--yolo"])
    return tmux.new_window(resolved)


# Claude's permission prompt is a numbered select list:
#   ❯ 1. Yes
#     2. Yes, and don't ask again …
#     3. No, and tell Claude what to do differently (esc)
# We drive it with the documented key shortcuts. "deny" uses Escape (cancel)
# rather than "3", which would drop the session into a "tell me what to do"
# text prompt.
_PERM_KEYS = {
    "approve": ["1"],
    "approve_always": ["2"],
    "deny": ["Escape"],
}


def respond_permission(pid: int, choice: str) -> dict:
    """Answer the permission prompt in `pid`'s tmux pane via a menu keypress."""
    keys = _PERM_KEYS.get(choice)
    if keys is None:
        return {"ok": False, "error": f"unknown choice '{choice}' (expected: {', '.join(_PERM_KEYS)})"}
    w = find_window(pid)
    if not w:
        return {"ok": False, "error": f"no window pid={pid}"}
    if not w.tty:
        return {"ok": False, "error": "no tty for this session"}
    pane = tmux.pane_for_tty(w.tty)
    if pane is None:
        return {"ok": False, "error": "session not in a tmux pane"}
    r = tmux.send_keys(pane, *keys)
    r["choice"] = choice
    return r


# A numbered menu option line, e.g. "❯ 1. Yes" or "  2. 绿色". The leading
# cursor/whitespace (incl. nbsp) is stripped; group 1 = number, group 2 = label.
_MENU_OPT_RE = re.compile(r"^[\s ❯>*]*(\d+)\.[\s ]+(.*\S)\s*$")


# A multiSelect option carries a checkbox prefix, e.g. "[ ] Red" / "[✔] Green".
# Group 1 = the marker char (empty/space ⇒ unchecked), group 2 = the real label.
_CHECKBOX_RE = re.compile(r"^\[([^\]]?)\]\s+(.*\S)\s*$")

# Tab/checkbox markers that head an AskUserQuestion picker: ☐ = an unanswered
# question tab, ☒ = an answered one. Either anchors the *current* question.
_HEADER_MARKS = ("☐", "☒")


def _is_tabbar(line: str) -> bool:
    """True for a picker's tab strip, e.g. '←  ☐ Colors  ✔ Submit  →'.

    Recognized by the '✔ Submit' tab beside a navigation arrow; kept out of the
    parsed prompt (it's chrome, not question text). The submit-review screen's
    'Ready to submit your answers?' has no arrow or ✔, so it is never stripped.
    """
    return "Submit" in line and "✔" in line and ("←" in line or "→" in line)


def _is_menu_hint(line: str) -> bool:
    """Footer / instruction chrome that must not bleed into an option's detail
    text when collecting the wrapped lines under a numbered option."""
    return any(s in line for s in (
        "to select", "Esc to cancel", "Enter to confirm", "Tab to amend",
        "ctrl+e to explain", "Do you want to proceed", "Ready to submit",
        "Enter to set", "to use this session", "↑/↓",
    ))


# Top-left / bottom-left corners of a box-drawn panel (square or rounded).
_BOX_TL = ("┌", "╭")
_BOX_BL = ("└", "╰")


def _strip_side_preview(lines: list[str]) -> list[str]:
    """Crop a right-aligned preview/description panel out of a picker capture.

    AskUserQuestion options that carry a preview render side-by-side: the option
    list on the left, a box-drawn panel on the right that Claude often folds with
    '✂ N lines hidden'. Captured into one pane, each option row also carries the
    panel border, e.g. '1. Per-item adaptive        ┌──────────┐'. Left as-is the
    border (and the '5 lines hidden' chrome) leaks into the parsed option labels.

    We locate the panel by its top-left and bottom-left corners sharing a column
    (> 0, so a full-width frame is ignored) and cut every row of that block at
    that column. Only the panel's own rows are touched, so the question text above
    and the footer below — which span the full width — are never truncated.
    """
    top_row = top_col = bot_row = None
    for i, ln in enumerate(lines):
        if top_row is None:
            j = min((ln.find(c) for c in _BOX_TL if ln.find(c) > 0), default=-1)
            if j > 0:
                top_row, top_col = i, j
        if top_row is not None and i >= top_row:
            if any(ln.find(c) == top_col for c in _BOX_BL):
                bot_row = i
    if top_row is None or bot_row is None or bot_row < top_row:
        return lines
    out = list(lines)
    for i in range(top_row, bot_row + 1):
        out[i] = out[i][:top_col].rstrip()
    return out


def parse_pane_menu(text: str) -> Optional[dict]:
    """Parse an interactive menu (AskUserQuestion picker / permission prompt /
    multiSelect submit-review) out of a captured tmux pane, or None if no menu
    is currently on screen.

    The pending question/permission is NOT written to the transcript until it's
    resolved, so the live screen is the only place its options exist. Returns
    {kind, prompt, options:[{num, label[, checked]}], multi}.

    For a multiSelect picker `multi` is True and each option carries a `checked`
    bool. There the digit key TOGGLES an option and Enter does NOT submit — Tab
    advances to the footer-less "Ready to submit your answers?" review screen,
    which this also parses (as a plain picker: "1. Submit answers / 2. Cancel").
    """
    if not text:
        return None
    lines = _strip_side_preview(text.split("\n"))
    footer_idx = None
    for i, ln in enumerate(lines):
        if "to select" in ln and "navigate" in ln:
            footer_idx = i  # last match wins — the current picker is the lowest
    proceed_idx = None
    for i, ln in enumerate(lines):
        if "Do you want to proceed" in ln:
            proceed_idx = i
    review_idx = None
    for i, ln in enumerate(lines):
        if "Ready to submit your answers" in ln:
            review_idx = i
    confirm_idx = None
    for i, ln in enumerate(lines):
        if "Enter to confirm" in ln:  # startup "resume from summary?" picker
            confirm_idx = i

    if footer_idx is not None:
        mode = "question"
    elif proceed_idx is not None:
        mode = "permission"
    elif review_idx is not None:
        mode = "review"
    elif confirm_idx is not None:
        mode = "confirm"
    else:
        return None

    if mode == "question":
        # Anchor to the current question: the last tab header above the footer,
        # so older pickers' options sitting in scrollback aren't merged in.
        header_idx = None
        for i in range(footer_idx):
            if any(mk in lines[i] for mk in _HEADER_MARKS):
                header_idx = i
        lo = (header_idx + 1) if header_idx is not None else 0
        hi = footer_idx
    elif mode == "permission":
        header_idx = proceed_idx
        lo, hi = proceed_idx + 1, len(lines)
    elif mode == "confirm":  # startup picker; options sit above the footer
        # Anchor the prompt to the divider rule just above the options so the
        # transcript text scrolled in above it isn't swept into the prompt.
        header_idx = next((i for i in range(confirm_idx - 1, -1, -1)
                           if _HRULE_RE.match(lines[i])), None)
        lo, hi = (header_idx + 1 if header_idx is not None else 0), confirm_idx
    else:  # review — multiSelect confirmation ("1. Submit answers / 2. Cancel")
        header_idx = next((i for i in range(review_idx + 1)
                           if "Review your answers" in lines[i]), review_idx)
        lo, hi = review_idx, len(lines)

    opt_lines: list[tuple[int, int, str]] = []  # (line_idx, num, label)
    for i in range(lo, hi):
        m = _MENU_OPT_RE.match(lines[i])
        if m:
            opt_lines.append((i, int(m.group(1)), m.group(2).strip()))
    if not opt_lines:
        return None

    # Start at the last option numbered 1 (current picker), then take the
    # contiguous increasing run; fall back to all captured options if the "1."
    # scrolled off entirely.
    start_pos = next((k for k in range(len(opt_lines) - 1, -1, -1) if opt_lines[k][1] == 1), 0)
    run = [opt_lines[start_pos]]
    for tup in opt_lines[start_pos + 1:]:
        if tup[1] == run[-1][1] + 1:
            run.append(tup)
        else:
            break
    first_opt_line = run[0][0]

    def _meaningful(s):
        core = s.replace(" ", "")
        if not core:
            return False
        # skip pure divider / box-drawing lines
        return not all(c in "-=." or 0x2014 <= ord(c) <= 0x2027 or 0x2500 <= ord(c) <= 0x257F for c in core)

    # multiSelect options carry a [ ]/[✔] checkbox; split it off the label so the
    # dashboard renders the checked state instead of a literal "[✔] Foo". Each
    # option's wrapped continuation lines (the multi-line description Claude
    # prints under the numbered title) become `detail`, so the dashboard shows
    # the whole option instead of just its first line.
    run_idx = [t[0] for t in run]

    def _detail_for(k: int) -> str:
        d_hi = run_idx[k + 1] if k + 1 < len(run_idx) else hi
        parts = []
        for i in range(run_idx[k] + 1, d_hi):
            ln = lines[i]
            if _MENU_OPT_RE.match(ln) or _is_tabbar(ln) or _is_menu_hint(ln):
                continue
            t = ln.replace("\xa0", " ").strip()
            if _meaningful(t):
                parts.append(t)
        return " ".join(parts).strip()

    options: list[dict] = []
    multi = False
    for k, (_, n, label) in enumerate(run):
        cb = _CHECKBOX_RE.match(label)
        if cb:
            multi = True
            opt = {"num": n, "label": cb.group(2).strip(),
                   "checked": bool(cb.group(1).strip())}
        else:
            opt = {"num": n, "label": label}
        detail = _detail_for(k)
        if detail:
            opt["detail"] = detail
        options.append(opt)

    if mode == "permission":
        prompt = lines[proceed_idx].replace("\xa0", " ").strip()
    else:
        start = header_idx if header_idx is not None else 0
        parts = []
        for i2 in range(start, first_opt_line):
            if _is_tabbar(lines[i2]):
                continue  # drop the "✔ Submit" tab strip from the question text
            t = lines[i2].replace("\xa0", " ").strip()
            for mk in _HEADER_MARKS:
                t = t.replace(mk, " ")
            t = t.strip()
            if _meaningful(t):
                parts.append(t)
        prompt = " ".join(parts).strip()
    return {
        "kind": "permission" if mode == "permission" else "question",
        "prompt": prompt,
        "options": options,
        "multi": multi,
    }


def _menu_markers_present(text: str) -> bool:
    """Whether a captured viewport shows a live answerable menu."""
    return (
        ("to select" in text and "navigate" in text)
        or ("Do you want to proceed" in text)
        or ("Ready to submit your answers" in text)  # multiSelect review screen (no footer)
        # Startup "resume from summary vs full" picker — a different footer than
        # the tool-permission menus. "Enter to confirm" marks a choice awaiting
        # an answer (informational overlays say "Esc to dismiss", not confirm).
        or ("Enter to confirm" in text)
    )


def pane_menu_active(tty: Optional[str]) -> Optional[bool]:
    """Whether `tty`'s pane currently shows an answerable menu; None if unknowable.

    Claude's session registry reports waitingFor="dialog open" for ANY open
    overlay — including informational ones like the /goal panel, which has no
    options and doesn't block the agent. This is the pane-level ground truth
    the triage uses to tell a real picker from such an overlay. The
    three-valued return matters: None (no tty / no pane / failed capture)
    means "can't verify", and callers should keep trusting the registry.
    """
    if not tty:
        return None
    pane = tmux.pane_for_tty(tty)
    if pane is None:
        return None
    cap = tmux.capture_pane(pane)
    if not cap["ok"]:
        return None
    return _menu_markers_present(cap["text"])


def get_pane_menu(pid: int) -> Optional[dict]:
    """Return the live interactive menu in `pid`'s pane, or None.

    Two captures: the visible viewport confirms a menu is *currently* active
    (an answered picker left in scrollback must not be reported), then a
    scrollback capture recovers options that scrolled above the fold.
    """
    w = find_window(pid)
    if not w or not w.tty:
        return None
    pane = tmux.pane_for_tty(w.tty)
    if pane is None:
        return None
    visible = tmux.capture_pane(pane)
    if not visible["ok"]:
        return None
    vis = visible["text"]
    if not _menu_markers_present(vis):
        return None
    full = tmux.capture_pane(pane, scrollback=80)
    return parse_pane_menu(full["text"] if full["ok"] else vis)


# A horizontal rule (the input box's top/bottom border); ≥8 box/dash chars only.
_HRULE_RE = re.compile(r"^\s*[─—\-]{8,}\s*$")
# A queued message line: indented "❯ <text>". The leading whitespace is what
# distinguishes it from the column-0 active prompt and the in-box input line.
_QUEUED_RE = re.compile(r"^\s+❯[ \t]+(\S.*?)\s*$")
# A /btw aside answer renders in a modal overlay: a ▔▔▔ (U+2594) top border, the
# echoed "/btw …" question, the answer body, then a "… Esc to close" footer. The
# ▔ border is distinct from the input box's ─ rule (_HRULE_RE), so it anchors the
# top; "Esc to close" anchors the bottom.
_BTW_TOP_RE = re.compile(r"^\s*▔{6,}\s*$")


def parse_pane_queue(text: str) -> list[str]:
    """Best-effort list of prompts Claude shows as queued, from a captured pane.

    Claude renders the 2nd-and-later queued messages as indented "❯ <text>"
    lines stacked directly above the input box (whose placeholder becomes "Press
    up to edit queued messages"). We take the contiguous block of such lines
    immediately above the box's top border. The *first* queued message renders
    inconsistently (column-0 "❯ …" mixed into the output, or indented with no
    "❯") and is not reliably recoverable, so it may be missed — by design the
    reliable half of the queue comes from core.promptqueue, not this scrape.
    """
    if not text:
        return []
    lines = text.split("\n")
    rules = [i for i, ln in enumerate(lines) if _HRULE_RE.match(ln)]
    if len(rules) < 2:
        return []
    top_border = rules[-2]  # the two lowest rules bracket the input box
    items: list[str] = []
    for i in range(top_border - 1, -1, -1):
        m = _QUEUED_RE.match(lines[i])
        if not m:
            break  # block is contiguous; first non-queued line ends it
        items.append(m.group(1).strip())
    items.reverse()
    return items


def get_pane_queue(pid: int) -> list[str]:
    """Queued-message texts scraped live from `pid`'s pane (empty on any miss)."""
    w = find_window(pid)
    if not w or not w.tty:
        return []
    pane = tmux.pane_for_tty(w.tty)
    if pane is None:
        return []
    cap = tmux.capture_pane(pane)
    if not cap["ok"]:
        return []
    return parse_pane_queue(cap["text"])


def parse_btw_overlay(text: str) -> Optional[dict]:
    """Extract {question, answer} for the *newest* /btw aside, or None.

    /btw answers show in an ephemeral overlay and are never written to the
    transcript, so scraping the pane is the only way to recover them. The overlay
    is a history carousel: firing several /btw stacks every question, but only the
    current (newest) aside's answer is shown. So we pair the LAST "/btw …" line
    with the answer block beneath it — taking the first would swallow later
    question lines into the answer.

    Only latch a *settled* answer: while Claude is still generating, the answer
    region is just an animated spinner ("✽ Answering…") and the footer lacks the
    "c to copy · f to fork" hints (you can't copy an unfinished answer). Requiring
    "to copy" in the footer gates out mid-generation frames — this also stops the
    animating spinner from defeating the archive's de-dupe. Best-effort otherwise:
    a long answer that scrolls the ▔ border off-screen is missed by design.
    """
    got = _btw_regions(text)
    if got is None:
        return None
    question, answer_lines = got
    answer = "\n".join(answer_lines)[:4000]
    if not answer:
        return None
    return {"question": question, "answer": answer}


def _btw_regions(text: str) -> Optional[tuple[str, list[str]]]:
    """The (question, visible-answer-lines) of a *settled* /btw overlay, or None.

    The overlay pins its ▔ border, the "/btw …" question line, and the footer in
    place while only the answer region scrolls, so this same anchor logic works at
    any scroll position — capture_full_btw_answer relies on that to walk a long
    answer window-by-window. Returns None when there is no settled overlay on the
    pane (no footer, mid-generation, or border/question scrolled off), which is
    also the signal that the overlay has been dismissed."""
    if not text:
        return None
    lines = text.split("\n")
    foot = next((i for i in range(len(lines) - 1, -1, -1)
                 if "Esc to close" in lines[i]), None)
    if foot is None:
        return None
    if "to copy" not in lines[foot]:
        return None  # answer still generating — don't latch a partial/spinner
    top = next((i for i in range(foot - 1, -1, -1)
                if _BTW_TOP_RE.match(lines[i])), None)
    if top is None:
        return None
    q_idx = next((i for i in range(foot - 1, top, -1)
                  if lines[i].lstrip().startswith("/btw")), None)
    if q_idx is None:
        return None
    question = lines[q_idx].strip()
    if question.startswith("/btw"):
        question = question[len("/btw"):].strip()
    answer_lines = [ln.strip() for ln in lines[q_idx + 1:foot] if ln.strip()]
    return question, answer_lines


def get_btw_answer(pid: int) -> Optional[dict]:
    """The /btw overlay currently on `pid`'s pane as {question, answer}, or None.

    The overlay covers the visible pane, so a plain capture (no scrollback, which
    would pull in stale pre-overlay content) is what we want. None on any miss.
    """
    w = find_window(pid)
    if not w or not w.tty:
        return None
    pane = tmux.pane_for_tty(w.tty)
    if pane is None:
        return None
    cap = tmux.capture_pane(pane)
    if not cap["ok"]:
        return None
    return parse_btw_overlay(cap["text"])


# The /btw overlay shows a long answer only a sliding window at a time, and the
# un-scrolled remainder is never emitted to the terminal (nor the transcript), so
# a single capture truncates it. To recover the whole answer we scroll the window
# to the bottom and stitch each frame. Empirically (Claude Code v2.1.x, 80x24):
# only ↑/↓ scroll — PgUp/PgDn are no-ops and Ctrl-D/Ctrl-F/Space *dismiss* the
# overlay; ↓ advances a few lines and clamps at the bottom (frame stops changing);
# ↑ clamps at the top. Crucially, once the overlay is gone, arrow keys fall
# through to the composer (history recall), so we re-verify the overlay is present
# before every keystroke and, if it has vanished, stop sending keys at once.
_BTW_SCROLL_MAX = 120          # hard cap on ↓ presses (far beyond any real answer)
_BTW_SCROLL_SETTLE = 0.18      # let the overlay redraw before re-capturing
_BTW_ANSWER_MAX = 20000        # sanity cap on a fully-stitched answer


def _stitch_btw(acc: list[str], window: list[str]) -> list[str]:
    """Append a later, overlapping scroll `window` onto `acc`, dropping the longest
    prefix of `window` that is already the suffix of `acc`. Equal frames (the
    window clamped at the bottom) leave `acc` unchanged — the caller's stop signal."""
    if not acc:
        return list(window)
    for o in range(min(len(acc), len(window)), 0, -1):
        if acc[-o:] == window[:o]:
            return acc + window[o:]
    return acc + window


def capture_full_btw_answer(pid: int) -> Optional[dict]:
    """The *complete* /btw aside on `pid`'s pane, scrolling the overlay to recover
    an answer taller than the visible window. None if no settled overlay is up.

    Injects ↓ keys into the live pane, so callers must gate this (see
    core.btwcapture): only run it for a not-yet-archived aside, off the hot path.
    """
    w = find_window(pid)
    if not w or not w.tty:
        return None
    pane = tmux.pane_for_tty(w.tty)
    if pane is None:
        return None
    first = _btw_regions(tmux.capture_pane(pane).get("text", ""))
    if first is None:
        return None  # no settled overlay — never touch the keyboard
    question, acc = first
    presses = 0
    overlay_alive = True
    while presses < _BTW_SCROLL_MAX:
        tmux.send_keys(pane, "Down")
        presses += 1
        time.sleep(_BTW_SCROLL_SETTLE)
        cur = _btw_regions(tmux.capture_pane(pane).get("text", ""))
        if cur is None:
            # Overlay dismissed mid-scroll: stop now and DO NOT restore — further
            # arrows would drive the composer's history instead of the overlay.
            overlay_alive = False
            break
        merged = _stitch_btw(acc, cur[1])
        if merged == acc:
            break  # window clamped at the bottom — whole answer captured
        acc = merged
    if overlay_alive:
        # Restore the user's view to the top: exactly as many ↑ as ↓ (both clamp,
        # so this lands back at the top). ↑ never dismisses the overlay.
        for _ in range(presses):
            tmux.send_keys(pane, "Up")
            time.sleep(_BTW_SCROLL_SETTLE)
    answer = "\n".join(acc)[:_BTW_ANSWER_MAX]
    return {"question": question, "answer": answer} if answer else None


# Keys the dashboard is allowed to send into an interactive menu (e.g. the
# AskUserQuestion picker). Restricted to navigation/selection tokens so the
# endpoint can't be used to type arbitrary input — free text goes through
# send_prompt instead.
_MENU_KEYS = {"Enter", "Escape", "Up", "Down", "Space", "Tab"} | {str(n) for n in range(0, 10)}


def send_menu_keys(pid: int, keys: list[str]) -> dict:
    """Send a short sequence of whitelisted menu keys into `pid`'s tmux pane.

    Used to answer the AskUserQuestion picker from the dashboard: a digit selects
    an option, Enter submits, Escape cancels.
    """
    if not keys:
        return {"ok": False, "error": "no keys"}
    if len(keys) > 12:
        return {"ok": False, "error": "too many keys"}
    bad = [k for k in keys if k not in _MENU_KEYS]
    if bad:
        return {"ok": False, "error": f"disallowed keys: {', '.join(bad)}"}
    w = find_window(pid)
    if not w:
        return {"ok": False, "error": f"no window pid={pid}"}
    if not w.tty:
        return {"ok": False, "error": "no tty for this session"}
    pane = tmux.pane_for_tty(w.tty)
    if pane is None:
        return {"ok": False, "error": "session not in a tmux pane"}
    return tmux.send_keys(pane, *keys)


# A /btw answer stays up as a modal overlay ("Esc to close") until dismissed —
# it does NOT auto-close after answering, and neither the read-only scrape nor
# the scroll-stitch capture (capture_full_btw_answer) closes it. Text pasted into
# a pane while that overlay is open is swallowed by the overlay, not the composer,
# so the *next* prompt after a /btw is silently lost ("回不到普通 prompt 模式").
# Detect the overlay by its distinctive ▔ top border together with the "Esc to
# close" footer — distinct enough from normal transcript output that we won't
# Escape (and so interrupt) a merely-working session.
_OVERLAY_CLOSE_HINT = "Esc to close"
_OVERLAY_DISMISS_TRIES = 4      # Escape + re-check, a few times
_OVERLAY_DISMISS_SETTLE = 0.15  # let the overlay tear down before re-capturing


def _answer_overlay_open(text: str) -> bool:
    """True if a dismissible answer overlay (a /btw aside) is covering the pane."""
    if not text or _OVERLAY_CLOSE_HINT not in text:
        return False
    return any(_BTW_TOP_RE.match(ln) for ln in text.split("\n"))


def _dismiss_answer_overlay(pane: str) -> None:
    """Escape a modal /btw answer overlay so a following prompt lands in the real
    composer instead of being eaten. Best-effort and self-limiting: only presses
    Escape while the overlay is actually detected (never on a clean/busy pane),
    and gives up quietly if capture fails or it won't clear."""
    for _ in range(_OVERLAY_DISMISS_TRIES):
        cap = tmux.capture_pane(pane)
        if not cap.get("ok") or not _answer_overlay_open(cap.get("text", "")):
            return
        tmux.send_keys(pane, "Escape")
        time.sleep(_OVERLAY_DISMISS_SETTLE)


def send_prompt(pid: int, text: str) -> dict:
    """Inject a single-line prompt into the tmux pane that owns `pid`'s session."""
    w = find_window(pid)
    if not w:
        return {"ok": False, "error": f"no window pid={pid}"}
    if not w.tty:
        return {"ok": False, "error": "no tty for this session"}
    pane = tmux.pane_for_tty(w.tty)
    if pane is None:
        return {"ok": False, "error": "session not in a tmux pane"}
    # v1 is single-line: fold any internal newlines into spaces.
    collapsed = " ".join((text or "").split("\n")).strip()
    if not collapsed:
        return {"ok": False, "error": "prompt is empty"}
    if len(collapsed) > _MAX_PROMPT_CHARS:
        return {"ok": False, "error": f"prompt too long (max {_MAX_PROMPT_CHARS} chars)"}
    # A /btw aside from a prior send may still be open over the pane; clear it
    # first so this prompt isn't swallowed by the overlay.
    _dismiss_answer_overlay(pane)
    # Codex's TUI swallows an Enter that arrives glued to the pasted text; give it
    # a settle delay (scaled by paste size — a big prompt needs longer to ingest)
    # so the prompt actually submits instead of sitting unsent in the composer,
    # and verify the composer emptied afterward. Claude needs neither (settle 0.0).
    is_codex = getattr(w, "platform", "claude") == "codex"
    settle = tmux.codex_enter_settle(len(collapsed)) if is_codex else 0.0
    return tmux.send_text(
        pane, collapsed, settle_before_enter=settle, verify_submit=is_codex
    )

"""Side-effectful actions: focus, fork, export, close, spawn, send-prompt."""
from __future__ import annotations

import os
import re
import signal
import shlex
import shutil
import subprocess
import tempfile
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
    """
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


def fork_session(pid: int) -> dict:
    """Open a new window and fork the session (new ID, inherits history)."""
    w = find_window(pid)
    if not w:
        return {"ok": False, "error": f"no window pid={pid}"}

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


def create_session(cwd: str) -> dict:
    """Spawn a new tmux window running `claude` in `cwd` (validated server-side)."""
    if not cwd or not cwd.strip():
        return {"ok": False, "error": "cwd is required"}
    resolved = os.path.expanduser(cwd.strip())
    if not os.path.isdir(resolved):
        return {"ok": False, "error": f"not a directory: {resolved}"}
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

    if footer_idx is not None:
        mode = "question"
    elif proceed_idx is not None:
        mode = "permission"
    elif review_idx is not None:
        mode = "review"
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

    # multiSelect options carry a [ ]/[✔] checkbox; split it off the label so the
    # dashboard renders the checked state instead of a literal "[✔] Foo".
    options: list[dict] = []
    multi = False
    for (_, n, label) in run:
        cb = _CHECKBOX_RE.match(label)
        if cb:
            multi = True
            options.append({"num": n, "label": cb.group(2).strip(),
                            "checked": bool(cb.group(1).strip())})
        else:
            options.append({"num": n, "label": label})

    def _meaningful(s):
        core = s.replace(" ", "")
        if not core:
            return False
        # skip pure divider / box-drawing lines
        return not all(c in "-=." or 0x2014 <= ord(c) <= 0x2027 or 0x2500 <= ord(c) <= 0x257F for c in core)

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
    active = (
        ("to select" in vis and "navigate" in vis)
        or ("Do you want to proceed" in vis)
        or ("Ready to submit your answers" in vis)  # multiSelect review screen (no footer)
    )
    if not active:
        return None
    full = tmux.capture_pane(pane, scrollback=80)
    return parse_pane_menu(full["text"] if full["ok"] else vis)


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
    return tmux.send_text(pane, collapsed)

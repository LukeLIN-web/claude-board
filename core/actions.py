"""Side-effectful actions: focus, fork, export, close, review."""
from __future__ import annotations

import os
import signal
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from .sessions import CLAUDE_HOME, find_window
from .transcripts import timeline, extract_plan_history, extract_skills_used, extract_memory_ops

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


def fork_session(pid: int) -> dict:
    """Open a new iTerm2 window and fork the session (new ID, inherits history)."""
    w = find_window(pid)
    if not w:
        return {"ok": False, "error": f"no window pid={pid}"}

    inner = f"cd {shlex.quote(w.cwd)} && claude --resume {shlex.quote(w.session_id)} --fork-session"
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
        "cwd": w.cwd,
        "session_id": w.session_id,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


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


_review_results: dict[int, dict] = {}


def _build_review_summary(transcript_path: str, limit: int = 40) -> str:
    """Extract last N turns as compact text for review prompt."""
    from .transcripts import timeline
    events = timeline(transcript_path, limit=limit)
    lines: list[str] = []
    for ev in events:
        kind = ev.get("kind", "")
        ts = (ev.get("ts") or "")[:19]
        if kind == "user_text":
            lines.append(f"[USER {ts}] {ev.get('text','')[:500]}")
        elif kind == "assistant_text":
            lines.append(f"[ASSISTANT {ts}] {ev.get('text','')[:500]}")
        elif kind == "tool_use":
            extra = ", ".join(f"{k}={v!r}" for k, v in list(ev.get("extra", {}).items())[:2])
            lines.append(f"[TOOL {ts}] {ev.get('tool','')}({extra})")
        elif kind == "tool_result":
            lines.append(f"[RESULT] {ev.get('text','')[:200]}")
    return "\n".join(lines)


def review_session_start(pid: int) -> dict:
    """Start a background `claude -p` review (non-interactive, no new window)."""
    w = find_window(pid)
    if not w:
        return {"ok": False, "error": f"no window pid={pid}"}
    if pid in _review_results and _review_results[pid].get("status") == "running":
        return {"ok": True, "status": "already_running"}

    name = w.name or w.project_name or "session"
    transcript = w.transcript_path or ""
    if not transcript:
        return {"ok": False, "error": "no transcript to review"}

    summary = _build_review_summary(transcript, limit=40)

    prompt = (
        f"请 review 以下 Claude Code session 的工作成果。\n"
        f"Session: {name}\n"
        f"CWD: {w.cwd}\n\n"
        f"## 最近对话记录\n\n{summary}\n\n"
        f"请检查：\n"
        f"1. 任务是否完成\n"
        f"2. 有无低级错误或遗漏\n"
        f"3. 有无安全问题\n"
        f"4. 给出结论：PASS（可以关闭） / FAIL（需要继续或修复） / PARTIAL（部分完成）\n"
        f"用中文回答，200字以内。"
    )

    prompt_file = Path(f"/tmp/fleet-review-{pid}.txt")
    prompt_file.write_text(prompt, encoding="utf-8")

    _review_results[pid] = {"status": "running", "name": name}

    import threading

    def _run():
        try:
            cmd = f'cat {shlex.quote(str(prompt_file))} | claude -p --output-format text'
            proc = subprocess.run(
                ["zsh", "-c", f"source ~/.zshrc 2>/dev/null; cd {shlex.quote(w.cwd)} && {cmd}"],
                capture_output=True, text=True, timeout=120,
            )
            _review_results[pid] = {
                "status": "done",
                "name": name,
                "verdict": proc.stdout.strip()[-3000:],
                "rc": proc.returncode,
                "error": proc.stderr.strip()[-500:] if proc.returncode != 0 else "",
            }
        except Exception as e:
            _review_results[pid] = {"status": "error", "name": name, "error": str(e)}
        finally:
            prompt_file.unlink(missing_ok=True)

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "status": "started", "name": name}


def review_session_result(pid: int) -> dict:
    """Get the result of a background review."""
    return _review_results.get(pid, {"status": "not_found"})


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


_REVIEW_PROMPT = "请 review 你刚才做的工作，检查是否有低级错误、遗漏、安全问题。列出发现的问题和修复建议。"


def review_session(pid: int) -> dict:
    """Open a new iTerm2 window, resume the session, and send a review prompt."""
    w = find_window(pid)
    if not w:
        return {"ok": False, "error": f"no window pid={pid}"}

    resume_cmd = f"cd {shlex.quote(w.cwd)} && claude --resume {shlex.quote(w.session_id)}"
    # AppleScript: open new window → type resume command → wait a bit → type review prompt
    escaped_resume = resume_cmd.replace('\\', '\\\\').replace('"', '\\"')
    escaped_review = _REVIEW_PROMPT.replace('\\', '\\\\').replace('"', '\\"')
    script = f'''tell application "iTerm2"
    activate
    set newWin to (create window with default profile)
    tell current session of newWin
        write text "{escaped_resume}"
        delay 3
        write text "{escaped_review}"
    end tell
end tell'''
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": proc.returncode == 0,
        "pid": pid,
        "session_id": w.session_id,
    }

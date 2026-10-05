"""Cross-session search over Claude + Codex transcripts using ripgrep."""
from __future__ import annotations

import json
import re
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional

from . import sessions
from .codex import CODEX_SESSIONS_DIR
from .sessions import PROJECTS_DIR

# rg's --max-count. With context on, rg still prints a match that falls in the
# last counted match's after-context, so the cap is applied again below.
_HITS_PER_FILE = 5
_CONTEXT_LINES = 3
_TIMEOUT = 15  # seconds; a search still running then returns nothing


def rg_command(query: str, *flags: str) -> Optional[list[str]]:
    """ripgrep for `query` over every Claude and Codex transcript, or None if
    neither directory exists. `flags` go after the transcript globs, so a glob
    among them takes precedence over those."""
    dirs = [str(d) for d in (PROJECTS_DIR, CODEX_SESSIONS_DIR) if d.exists()]
    if not dirs:
        return None
    return ["rg", "-S", "-g", "*.jsonl", "-g", "!*.wakatime", *flags, query, *dirs]


def _extract_text(d: dict) -> str:
    t = d.get("type")
    msg = d.get("message") or {}
    # Claude Code format
    if t == "user":
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    return c.get("text") or ""
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    val = c.get("content")
                    if isinstance(val, list):
                        return " ".join(x.get("text", "") for x in val if isinstance(x, dict))
                    return str(val)
        return ""
    if t == "assistant":
        content = msg.get("content")
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    return c.get("text") or ""
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    return f"<tool:{c.get('name')}>"
        return ""
    # A prompt sent while the session was busy: Claude writes no user row for it
    # (see transcripts._flatten_queued_prompt), so without these two the only
    # hits for it are raw jsonl lines rendered as JSON.
    if t == "attachment":
        a = d.get("attachment") or {}
        return (a.get("prompt") or "") if a.get("type") == "queued_command" else ""
    if t == "queue-operation":
        return d.get("content") or ""
    # Codex format
    if t == "event_msg":
        payload = d.get("payload") or {}
        content = payload.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "input_text":
                    return c.get("text") or ""
        return ""
    if t == "response_item":
        payload = d.get("payload") or {}
        content = payload.get("content")
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "output_text":
                    return (c.get("text") or "")[:500]
        return ""
    return ""


def _extract_type_label(d: dict) -> str:
    t = d.get("type", "")
    if t == "user":
        return "user"
    if t == "assistant":
        return "assistant"
    if t == "attachment" and (d.get("attachment") or {}).get("type") == "queued_command":
        return "user:queued"
    if t == "queue-operation":
        return f"queue:{d.get('operation', '')}"
    if t == "event_msg":
        role = (d.get("payload") or {}).get("role", "")
        return f"codex:{role}" if role else "codex:event"
    if t == "response_item":
        return "codex:response"
    return t or "unknown"


def _detect_platform(path: Path) -> str:
    if str(CODEX_SESSIONS_DIR) in str(path):
        return "codex"
    return "claude"


def _row(raw: str) -> dict:
    try:
        d = json.loads(raw)
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def _file_hits(path: Path, lines: dict[int, str], matched: list[int], query: str) -> list[dict]:
    """One file's hits, from the lines rg printed for it: each match plus the
    context around it."""
    platform = _detect_platform(path)
    # Hide hits from projects filtered out by CLAUDE_FLEET_CWD_INCLUDE/
    # EXCLUDE, judged on the cwd the transcript records (its projects/<slug>
    # name is lossy); codex sessions aren't cwd-addressable, so they're left
    # untouched.
    if platform == "claude" and not sessions.transcript_visible(path):
        return []
    rows = {n: _row(raw) for n, raw in lines.items()}
    hits: list[dict] = []
    for line_no in matched:
        d = rows[line_no]
        text = _extract_text(d).strip()
        if not text:
            text = lines[line_no].strip()[:200]
        context: list[dict] = []
        for i in range(max(1, line_no - _CONTEXT_LINES), line_no + _CONTEXT_LINES + 1):
            ctx = _extract_text(rows.get(i) or {}).strip()
            if ctx:
                context.append({
                    "line": i,
                    "type": _extract_type_label(rows[i]),
                    "text": ctx[:300],
                    "is_match": i == line_no,
                })
        hits.append({
            "path": str(path),
            "line": line_no,
            "project_slug": path.parent.name,
            "session_id": path.stem,
            "ts": d.get("timestamp") or "",
            "excerpt": excerpt(text, query),
            "platform": platform,
            "context": context,
        })
    return hits


def search(query: str, limit: int = 60) -> list[dict]:
    if not query.strip():
        return []

    cmd = rg_command(query, "--json", "--max-count", str(_HITS_PER_FILE),
                     "-C", str(_CONTEXT_LINES))
    if not cmd:
        return []

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
    except FileNotFoundError:
        return []
    # Read rg as it goes and stop it at `limit` hits: a common word matches in
    # thousands of files, and waiting for all of them cost about a second for
    # hits nobody would be shown.
    expired = threading.Event()
    timer = threading.Timer(_TIMEOUT, lambda: (expired.set(), proc.kill()))

    # rg prints each file's records together, begin to end. Its match and
    # context records carry the line itself, so a hit and the lines around it
    # come straight from here rather than from reading the file again.
    hits: list[dict] = []
    lines: dict[int, str] = {}
    matched: list[int] = []
    with proc:
        timer.start()
        try:
            for out in proc.stdout:
                try:
                    rec = json.loads(out)
                except Exception:
                    continue
                kind, data = rec.get("type"), rec.get("data") or {}
                if kind == "begin":
                    lines, matched = {}, []
                elif kind in ("match", "context"):
                    line_no = data.get("line_number")
                    if not line_no:
                        continue
                    lines[line_no] = (data.get("lines") or {}).get("text") or ""
                    if kind == "match" and len(matched) < _HITS_PER_FILE:
                        matched.append(line_no)
                elif kind == "end" and matched:
                    path = (data.get("path") or {}).get("text")
                    if path:
                        hits += _file_hits(Path(path), lines, matched, query)
                    if len(hits) >= limit:
                        break
        finally:
            timer.cancel()
            proc.kill()
    if expired.is_set():
        return []
    hits = hits[:limit]
    hits.sort(key=lambda h: h.get("ts") or "", reverse=True)
    return hits


def excerpt(text: str, query: str, span: int = 120,
            tidy: Callable[[str], str] = str) -> str:
    """`text` cut to `span` chars around the first match of `query`, a "…"
    marking each cut. `tidy` runs on the cut before the marks go on."""
    if not text:
        return ""
    m = re.search(re.escape(query), text, re.IGNORECASE)
    if not m:
        return text[: span * 2]
    start = max(0, m.start() - span // 2)
    end = min(len(text), m.end() + span // 2)
    snippet = tidy(text[start:end])
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet

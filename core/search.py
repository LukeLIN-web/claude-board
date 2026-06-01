"""Cross-session search over Claude + Codex transcripts using ripgrep."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Optional

from .sessions import CLAUDE_HOME, HOME_BASE, PROJECTS_DIR

CODEX_HOME = HOME_BASE / ".codex"
CODEX_SESSIONS_DIR = CODEX_HOME / "sessions"


def _project_slug_from_file(path: Path) -> str:
    return path.parent.name


def _session_id_from_file(path: Path) -> str:
    return path.stem


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
    # Codex format
    if t == "event_msg":
        payload = d.get("payload") or {}
        role = payload.get("role", "")
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
    if t == "event_msg":
        role = (d.get("payload") or {}).get("role", "")
        return f"codex:{role}" if role else "codex:event"
    if t == "response_item":
        return "codex:response"
    return t or "unknown"


def _read_context(path: Path, center_line: int, radius: int = 3) -> list[dict]:
    """Read ±radius lines around center_line, extract structured context."""
    context: list[dict] = []
    start = max(1, center_line - radius)
    end = center_line + radius
    try:
        with path.open() as f:
            for i, line in enumerate(f, start=1):
                if i < start:
                    continue
                if i > end:
                    break
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                text = _extract_text(d).strip()
                if not text:
                    continue
                context.append({
                    "line": i,
                    "type": _extract_type_label(d),
                    "text": text[:300],
                    "is_match": i == center_line,
                })
    except Exception:
        pass
    return context


def _detect_platform(path: Path) -> str:
    if str(CODEX_SESSIONS_DIR) in str(path):
        return "codex"
    return "claude"


def search(query: str, limit: int = 60) -> list[dict]:
    if not query.strip():
        return []

    search_dirs: list[str] = []
    if PROJECTS_DIR.exists():
        search_dirs.append(str(PROJECTS_DIR))
    if CODEX_SESSIONS_DIR.exists():
        search_dirs.append(str(CODEX_SESSIONS_DIR))
    if not search_dirs:
        return []

    cmd = [
        "rg", "--json", "-i", "-S",
        "--max-count", "5",
        "-g", "*.jsonl",
        "-g", "!*.wakatime",
        query,
    ] + search_dirs

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    hits: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for line in proc.stdout.splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") != "match":
            continue
        data = rec.get("data") or {}
        path_info = data.get("path") or {}
        text_info = data.get("lines") or {}
        path = path_info.get("text")
        line_no = data.get("line_number")
        if not path or not line_no:
            continue
        key = (path, line_no)
        if key in seen:
            continue
        seen.add(key)

        p = Path(path)
        raw = _read_line(p, line_no) or (text_info.get("text") or "")
        try:
            d = json.loads(raw)
        except Exception:
            d = {}
        text = _extract_text(d).strip()
        if not text:
            text = (text_info.get("text") or "").strip()[:200]

        context = _read_context(p, line_no, radius=3)

        hits.append({
            "path": path,
            "line": line_no,
            "project_slug": _project_slug_from_file(p),
            "session_id": _session_id_from_file(p),
            "ts": d.get("timestamp") or "",
            "type": d.get("type") or "",
            "excerpt": _excerpt(text, query),
            "permission_mode": d.get("permissionMode"),
            "platform": _detect_platform(p),
            "context": context,
        })
        if len(hits) >= limit:
            break
    hits.sort(key=lambda h: h.get("ts") or "", reverse=True)
    return hits


def _read_line(path: Path, line_no: int) -> Optional[str]:
    try:
        with path.open() as f:
            for i, line in enumerate(f, start=1):
                if i == line_no:
                    return line.rstrip("\n")
    except Exception:
        return None
    return None


def _excerpt(text: str, query: str, span: int = 120) -> str:
    if not text:
        return ""
    m = re.search(re.escape(query), text, re.IGNORECASE)
    if not m:
        return text[: span * 2]
    start = max(0, m.start() - span // 2)
    end = min(len(text), m.end() + span // 2)
    snippet = text[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet

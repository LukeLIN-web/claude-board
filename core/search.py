"""Cross-session search over Claude + Codex transcripts using ripgrep."""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from . import sessions
from .codex import CODEX_SESSIONS_DIR
from .sessions import PROJECTS_DIR
from .transcripts import _parse_ts

# rg's --max-count. With context on, rg still prints a match that falls in the
# last counted match's after-context, so the cap is applied again below.
_HITS_PER_FILE = 5
_CONTEXT_LINES = 3
_TIMEOUT = 15  # seconds; a search still running then returns nothing


def rg_command(query: str, *flags: str,
               paths: Optional[list[str]] = None) -> Optional[list[str]]:
    """ripgrep for `query` over every Claude and Codex transcript (or over just
    `paths`), or None if neither directory exists. `flags` go after the
    transcript globs, so a glob among them takes precedence over those.

    The query is literal text (-F) handed over with -e. What gets typed into a
    search box is words, not a regex: as a bare pattern `foo(` was a regex
    parse error and found nothing, `a.b` also found `axb`, and `--resume` was
    read as one of rg's own flags. Case is rg's smart case (-S), and `find`
    places a match in the text by the same rule."""
    dirs = [str(d) for d in (PROJECTS_DIR, CODEX_SESSIONS_DIR) if d.exists()]
    if not dirs:
        return None
    return ["rg", "-S", "-F", "-g", "*.jsonl", "-g", "!*.wakatime", *flags,
            "-e", query, *(dirs if paths is None else paths)]


def find(text: str, query: str) -> Optional[re.Match]:
    """Where `query` first occurs in `text`, matched the way rg_command matches
    it: as literal text, under rg's smart case — any case while the query is
    all lower case, exactly once it has a capital in it. An excerpt located
    ignoring case put a search for "Edit" on the first "edit" in the line, not
    on the "Edit" rg had matched."""
    flags = 0 if any(c.isupper() for c in query) else re.IGNORECASE
    return re.search(re.escape(query), text, flags)


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


def _newest_files(query: str, limit: int, deadline: float) -> dict[str, float]:
    """The `limit` most recently written transcripts that hold `query` and that
    the cwd filter shows, newest first, each with its mtime."""
    cmd = rg_command(query, "-l", "--null")
    if not cmd:
        return {}
    try:
        out = subprocess.run(cmd, capture_output=True,
                             timeout=max(0.0, deadline - time.monotonic())).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    mtimes: dict[str, float] = {}
    for raw in out.split(b"\0"):
        if raw:
            path = os.fsdecode(raw)
            try:
                mtimes[path] = os.stat(path).st_mtime
            except OSError:
                continue
    newest: dict[str, float] = {}
    for path in sorted(mtimes, key=mtimes.__getitem__, reverse=True):
        # Projects filtered out by CLAUDE_FLEET_CWD_INCLUDE/EXCLUDE are judged
        # on the cwd the transcript records (its projects/<slug> name is
        # lossy) — a Codex rollout's included, in its session_meta. Skipped
        # here, before the cut, or a host that hides its busiest projects would
        # fill all `limit` places with files that yield nothing.
        if not sessions.transcript_visible(path):
            continue
        newest[path] = mtimes[path]
        if len(newest) >= limit:
            break
    return newest


def search(query: str, limit: int = 60) -> list[dict]:
    """The `limit` newest hits for `query`, newest first, at most
    _HITS_PER_FILE of them from any one transcript."""
    if not query.strip():
        return []
    deadline = time.monotonic() + _TIMEOUT

    # The newest hits, without reading every hit there is. rg searches files
    # in parallel and prints each as its thread finishes, so the first
    # `limit` hits it printed were an arbitrary set, and sorting them by time
    # afterwards could not bring back the newer ones it had not reached. Two
    # passes instead: `rg -l` names the files the query occurs in (still in
    # parallel, and each file is dropped at its first match), they are put in
    # order of last write, and a single-threaded rg — which keeps the order
    # it is given — reads the newest of them first. A common word occurs in
    # thousands of files, and collecting every hit and sorting them cost about
    # a second for hits nobody would be shown. Each file holding the query has
    # at least one hit, so the newest `limit` files always hold `limit` hits.
    files = _newest_files(query, limit, deadline)
    if not files:
        return []
    cmd = rg_command(query, "-j1", "--json", "--max-count", str(_HITS_PER_FILE),
                     "-C", str(_CONTEXT_LINES), paths=list(files))
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
    except OSError:
        return []
    expired = threading.Event()
    timer = threading.Timer(max(0.0, deadline - time.monotonic()),
                            lambda: (expired.set(), proc.kill()))

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
                    # A row is never newer than its file's last write. Once
                    # there are `limit` hits and the next file was last
                    # written before the limit-th newest of them, neither it
                    # nor any file after it has a hit that would make the cut.
                    path = (data.get("path") or {}).get("text")
                    if len(hits) >= limit:
                        cutoff = sorted(h["ts"] for h in hits)[-limit]
                        if files.get(path, 0.0) <= _parse_ts(cutoff):
                            break
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
        finally:
            timer.cancel()
            proc.kill()
    if expired.is_set():
        return []
    hits.sort(key=lambda h: h["ts"], reverse=True)
    return hits[:limit]


def excerpt(text: str, query: str, span: int = 120,
            tidy: Callable[[str], str] = str) -> str:
    """`text` cut to `span` chars around the first match of `query`, a "…"
    marking each cut. `tidy` runs on the cut before the marks go on."""
    if not text:
        return ""
    m = find(text, query)
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

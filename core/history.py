"""Index all past sessions from history.jsonl + projects/**/*.jsonl + codex."""
from __future__ import annotations

import json
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, asdict, field
from itertools import islice
from pathlib import Path
from typing import Optional

from .codex import list_codex_sessions
from .opencode import list_opencode_sessions, search_opencode
from .search import excerpt, rg_command
from .sessions import CLAUDE_HOME, PROJECTS_DIR, _cwd_visible, list_windows
from .transcripts import (
    _iter_lines,
    _row_model,
    clean_user_text,
    is_injected_text,
    is_injected_user_row,
    memo_by_file,
    session_activity,
)

HISTORY_JSONL = CLAUDE_HOME / "history.jsonl"


@dataclass
class HistorySession:
    session_id: str
    project: str
    project_name: str
    first_input: str
    first_ts: str
    last_ts: str
    transcript_path: Optional[str]
    transcript_size: int
    transcript_mtime: int
    is_alive: bool
    platform: str = "claude"
    model: str = ""
    skills_used: list = field(default_factory=list)
    memory_ops: list = field(default_factory=list)
    skill_breakdown: dict = field(default_factory=dict)
    memory_breakdown: dict = field(default_factory=dict)


_cache: list[HistorySession] = []
_cache_ts: float = 0
_CACHE_TTL = 30

# Per-transcript enrichment (skills/memory/model/first-input) costs a full read
# of each .jsonl. With ~1k sessions that made a cold index build take 15s+, and
# it ran on every cache miss. We now only enrich the most-recent sessions (the
# History panel shows recent sessions; older ones still appear as cheap
# skeletons), and every read below is memoized on the transcript's (mtime,
# size) (transcripts.memo_by_file, which keeps room for this many), so the
# periodic rebuild never re-parses an unchanged transcript.
_ENRICH_LIMIT = 200


def _load_history_jsonl() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not HISTORY_JSONL.exists():
        return out
    try:
        with HISTORY_JSONL.open(errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                sid = d.get("sessionId", "")
                if not sid:
                    continue
                display = d.get("display", "")
                ts = d.get("timestamp", "")
                project = d.get("project", "")
                if sid not in out:
                    out[sid] = {
                        "first_input": display[:300],
                        "first_ts": ts,
                        "last_ts": ts,
                        "project": project,
                    }
                out[sid]["last_ts"] = ts
    except Exception:
        pass
    return out


def _scan_transcripts() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not PROJECTS_DIR.exists():
        return out
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        for f in proj_dir.glob("*.jsonl"):
            sid = f.stem
            try:
                st = f.stat()
            except Exception:
                continue
            out[sid] = {
                "path": str(f),
                "size": st.st_size,
                "mtime": int(st.st_mtime * 1000),
                "project_slug": proj_dir.name,
            }
    return out


def _build_index() -> list[HistorySession]:
    hist = _load_history_jsonl()
    transcripts = _scan_transcripts()
    alive = {w.session_id for w in list_windows() if w.session_id}

    all_sids = set(hist.keys()) | set(transcripts.keys())
    sessions: list[HistorySession] = []

    # Phase 1 — cheap skeletons (no per-transcript parsing). first_input comes
    # from history.jsonl here; the transcript-read fallback is deferred to the
    # enrichment phase so we never read 1k transcripts just to list them.
    for sid in all_sids:
        h = hist.get(sid, {})
        t = transcripts.get(sid, {})

        project = h.get("project", "")
        project_name = project.rsplit("/", 1)[-1] if project else (
            t.get("project_slug", "").replace("-", "/").split("/")[-1] or "unknown"
        )

        sessions.append(HistorySession(
            session_id=sid,
            project=project,
            project_name=project_name,
            first_input=h.get("first_input", ""),
            first_ts=h.get("first_ts", ""),
            last_ts=h.get("last_ts", ""),
            transcript_path=t.get("path"),
            transcript_size=t.get("size", 0),
            transcript_mtime=t.get("mtime", 0),
            is_alive=sid in alive,
        ))

    # Merge Codex and OpenCode sessions
    for source in (list_codex_sessions, list_opencode_sessions):
        try:
            sessions.extend(HistorySession(**s) for s in source())
        except Exception:
            pass

    sessions.sort(key=lambda s: s.transcript_mtime or 0, reverse=True)

    # Phase 2 — enrich only the most-recent claude sessions (bounded + memoized).
    for s in sessions[:_ENRICH_LIMIT]:
        if s.platform == "claude" and s.transcript_path:
            _apply_enrichment(s)
    return sessions


def _apply_enrichment(s: HistorySession) -> None:
    """Fill skills/memory/model/first-input on `s` from its transcript."""
    tp = Path(s.transcript_path)
    activity = session_activity(tp)
    s.first_input = s.first_input or _extract_first_user_text(tp)
    s.model = _extract_model(tp)
    s.skills_used = activity["skills_used"]
    s.memory_ops = activity["memory_ops"]
    s.skill_breakdown = activity["skill_breakdown"]
    s.memory_breakdown = activity["memory_breakdown"]


# How far to look for a title before settling for what's been found. The first
# real prompt sits within a handful of rows in practice; the bound is only here
# so a session that never typed one doesn't read its whole transcript.
_TITLE_SCAN_LINES = 400


@memo_by_file
def _extract_first_user_text(path: Path) -> str:
    """What the human first asked this session for — the card's title.

    Two kinds of row have to be walked past to reach it, and both used to win by
    being early:

      - rows the harness wrote (`is_injected_user_row`). A session that opens
        with a slash command gets the local-command caveat as its first `user`
        row, which titled about a third of the fleet's cards with "Caveat: The
        messages below were generated by the user…".
      - the slash command itself. It *was* typed, but "clear" says nothing about
        what the session is for, so it's kept only as a fallback for a session
        that never said anything else.
    """
    fallback = ""
    for d in islice(_iter_lines(path), _TITLE_SCAN_LINES):
        if d.get("type") != "user" or is_injected_user_row(d):
            continue
        content = (d.get("message") or {}).get("content", [])
        if isinstance(content, str):
            texts = [content]
        elif isinstance(content, list):
            texts = [c.get("text") or "" for c in content
                     if isinstance(c, dict) and c.get("type") == "text"]
        else:
            continue
        for raw in texts:
            if not raw.strip() or is_injected_text(raw):
                continue
            cleaned = clean_user_text(raw)[:300]
            if "<command-name>" in raw:
                fallback = fallback or cleaned
            else:
                return cleaned
    return fallback


@memo_by_file
def _extract_model(path: Path) -> str:
    """The model the session started on (transcripts.current_model is the one
    it is on now)."""
    for d in _iter_lines(path):
        model = _row_model(d)
        if model:
            return model
    return ""


def _rg_search_sessions(query: str) -> dict[str, list[str]]:
    """Use ripgrep to find session IDs + match snippets.

    Returns {session_id: [snippet1, snippet2, ...]}.
    """
    cmd = rg_command(query, "--max-count", "3", "-g", "!*subagents*", "--no-heading")
    if not cmd:
        return {}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    result: dict[str, list[str]] = {}
    for raw_line in proc.stdout.splitlines():
        # format: /path/to/sid.jsonl:jsonl_content
        colon = raw_line.find(".jsonl:")
        if colon < 0:
            continue
        sid = Path(raw_line[:colon + 6]).stem
        content = raw_line[colon + 7:]
        snippets = result.setdefault(sid, [])
        if len(snippets) < 3:
            snippets.append(excerpt(content, query, tidy=_one_line))
    return result


def _one_line(raw: str) -> str:
    """A slice of a raw jsonl line as one line of text: real and escaped
    newlines both read as spaces."""
    return raw.replace("\n", " ").replace("\\n", " ").strip()


def index() -> list[HistorySession]:
    """Every past session, newest first (rebuilt at most every _CACHE_TTL
    seconds). The sessions are shared, so callers must not change them.

    The machine-local cwd visibility filter (CLAUDE_FLEET_CWD_INCLUDE/EXCLUDE)
    applies here, up front, so every consumer — the History panel, Skills/Memory
    reverse-lookups + counts, and resume/fork (which resolve sessions through
    here) — only ever sees and acts on visible projects."""
    global _cache, _cache_ts
    now = time.time()
    if now - _cache_ts > _CACHE_TTL or not _cache:
        _cache = _build_index()
        _cache_ts = now
    return [s for s in _cache if _cwd_visible(s.project)]


def get(session_id: str) -> Optional[HistorySession]:
    """The visible session `session_id`, or None when the index doesn't know it."""
    return next((s for s in index() if s.session_id == session_id), None)


def list_sessions(q: Optional[str] = None, page: int = 1, limit: int = 30) -> dict:
    filtered = index()
    rg_matches: dict[str, list[str]] = {}
    if q:
        ql = q.lower()
        meta_sids = {
            s.session_id for s in filtered
            if ql in s.first_input.lower()
            or ql in s.project_name.lower()
            or ql in s.session_id.lower()
            or ql in s.project.lower()
            or ql in (s.transcript_path or "").lower()
        }
        rg_matches = _rg_search_sessions(q)
        # Also search OpenCode SQLite
        try:
            oc_matches = search_opencode(q)
            for sid, snips in oc_matches.items():
                if sid not in rg_matches:
                    rg_matches[sid] = snips
                else:
                    rg_matches[sid].extend(snips[:2])
        except Exception:
            pass
        all_sids = meta_sids | set(rg_matches.keys())
        filtered = [s for s in filtered if s.session_id in all_sids]

    total = len(filtered)
    start = (page - 1) * limit
    page_items = filtered[start : start + limit]

    sessions_out = []
    for s in page_items:
        d = asdict(s)
        d["match_snippets"] = rg_matches.get(s.session_id, [])
        sessions_out.append(d)
    return {
        "total": total,
        "page": page,
        "limit": limit,
        "sessions": sessions_out,
    }


# What each session's breakdown counts, keyed by its field there and named as
# the reverse-lookup rows below name it. The index produces these per session,
# for Claude, OpenCode and Codex alike.
SKILL_KINDS = {"per_skill_invokes": "invoke", "per_skill_reads": "reads",
               "per_skill_writes": "writes", "per_skill_bash_refs": "bash_refs"}
MEMORY_KINDS = {"per_memory_reads": "reads", "per_memory_writes": "writes",
                "per_memory_edits": "edits"}


def sessions_touching(name: str, breakdown_key: str, kinds: dict[str, str]) -> dict:
    """Reverse lookup: the sessions whose `breakdown_key` counts `name` under
    any of `kinds`, busiest first, with the per-kind counts."""
    rows = []
    for s in index():
        bd = getattr(s, breakdown_key) or {}
        counts = {row: (bd.get(k) or {}).get(name, 0) for k, row in kinds.items()}
        total = sum(counts.values())
        if total == 0:
            continue
        rows.append({
            "session_id": s.session_id,
            "project_name": s.project_name,
            "platform": s.platform,
            "title": s.first_input[:120],
            "ts": s.last_ts or s.first_ts or "",
            **counts,
            "total": total,
        })
    rows.sort(key=lambda r: -r["total"])
    return {"name": name, "sessions": rows, "session_count": len(rows)}


def skill_totals() -> tuple[Counter, dict[str, Counter]]:
    """Over every session: how many invoked each skill, and each SKILL_KINDS
    count summed per skill, keyed by that kind's row name."""
    session_count: Counter[str] = Counter()
    activity = {row: Counter() for row in SKILL_KINDS.values()}
    for s in index():
        session_count.update(s.skills_used)
        bd = s.skill_breakdown or {}
        for k, row in SKILL_KINDS.items():
            activity[row].update(bd.get(k) or {})
    return session_count, activity


def memory_session_counts() -> tuple[Counter, Counter]:
    """How many sessions read each memory, and how many wrote or edited it."""
    reads: Counter[str] = Counter()
    writes: Counter[str] = Counter()
    for s in index():
        for m in s.memory_ops:
            (reads if m["operation"] == "read" else writes)[m["name"]] += 1
    return reads, writes


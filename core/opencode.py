"""Parse OpenCode sessions from SQLite DB at ~/.local/share/opencode/opencode.db."""
from __future__ import annotations

import datetime
import json
import sqlite3
from collections import Counter
from typing import Optional

from .search import excerpt, find
from .sessions import HOME_BASE
from .textcap import MESSAGE_CHARS, TOOL_ARG_CHARS, TOOL_RESULT_CHARS, cap_text
from .transcripts import _SKILL_PATH_RE, event

OPENCODE_DB = HOME_BASE / ".local/share/opencode/opencode.db"


def _get_conn() -> Optional[sqlite3.Connection]:
    if not OPENCODE_DB.exists():
        return None
    try:
        return sqlite3.connect(str(OPENCODE_DB), timeout=3)
    except Exception:
        return None


def list_opencode_sessions() -> list[dict]:
    conn = _get_conn()
    if not conn:
        return []
    try:
        cur = conn.execute("""
            SELECT s.id, s.title, s.directory, s.time_created, s.time_updated,
                   (SELECT substr(json_extract(p.data, '$.text'), 1, 300)
                    FROM part p JOIN message m ON p.message_id = m.id
                    WHERE m.session_id = s.id
                      AND json_extract(m.data, '$.role') = 'user'
                      AND json_extract(p.data, '$.type') = 'text'
                    ORDER BY p.time_created ASC LIMIT 1
                   ) as first_input,
                   (SELECT json_extract(m2.data, '$.model.providerID') || '/' || json_extract(m2.data, '$.model.modelID')
                    FROM message m2
                    WHERE m2.session_id = s.id AND json_extract(m2.data, '$.model') IS NOT NULL
                    ORDER BY m2.time_created DESC LIMIT 1
                   ) as model
            FROM session s
            ORDER BY s.time_updated DESC
        """)
        rows = cur.fetchall()
        calls = _tool_calls_by_session(conn)
    except Exception:
        return []
    finally:
        conn.close()

    sessions = []
    for row in rows:
        sid, title, directory, created, updated, first_input, model = row
        project_name = directory.rsplit("/", 1)[-1] if directory else "opencode"
        sessions.append({
            "session_id": sid,
            "project": directory or "",
            "project_name": project_name,
            "first_input": (first_input or title or "")[:300],
            "first_ts": _ms_to_iso(created),
            "last_ts": _ms_to_iso(updated),
            "transcript_path": None,
            "transcript_size": 0,
            "transcript_mtime": updated or 0,
            "is_alive": False,
            "platform": "opencode",
            "model": model or "",
            **_activity(calls.get(sid, [])),
        })
    return sessions


def session_directory(session_id: str) -> Optional[str]:
    """The directory OpenCode session `session_id` ran in ("" if it records
    none), or None when there is no such session."""
    conn = _get_conn()
    if not conn:
        return None
    try:
        row = conn.execute("SELECT directory FROM session WHERE id = ?",
                           (session_id,)).fetchone()
    except Exception:
        return None
    finally:
        conn.close()
    return (row[0] or "") if row else None


def opencode_timeline(session_id: str, limit: int = 2000) -> list[dict]:
    conn = _get_conn()
    if not conn:
        return []
    try:
        cur = conn.execute("""
            SELECT json_extract(m.data, '$.role') as role,
                   p.data as part_data, p.time_created as part_time
            FROM message m
            JOIN part p ON p.message_id = m.id
            WHERE m.session_id = ?
            ORDER BY p.time_created ASC
        """, (session_id,))
        rows = cur.fetchall()
    except Exception:
        return []
    finally:
        conn.close()

    events: list[dict] = []
    for role, part_data_str, part_time in rows:
        try:
            pd = json.loads(part_data_str)
        except Exception:
            continue
        ptype = pd.get("type", "")
        ts = _ms_to_iso(part_time)

        if ptype == "text":
            text = pd.get("text", "")
            if not text.strip():
                continue
            kind = "user_text" if role == "user" else "assistant_text"
            events.append(event(ts, kind, cap_text(text, MESSAGE_CHARS), role=role or "assistant"))

        elif ptype == "tool":
            state = pd.get("state") or {}
            status, output = state.get("status", ""), state.get("output")
            # A call that ended any other way (an error) with output gets no row.
            if output and status not in ("completed", "running"):
                continue
            events.append(event(ts, "tool_use", role="assistant", tool=pd.get("tool", ""),
                                extra=_tool_preview(state.get("input") or {})))
            if output and status == "completed":
                events.append(event(ts, "tool_result", cap_text(output, TOOL_RESULT_CHARS),
                                    role="user"))

    return events[-limit:]


def search_opencode(query: str) -> dict[str, list[str]]:
    """Search OpenCode parts for a query. Returns {session_id: [snippets]}."""
    conn = _get_conn()
    if not conn:
        return {}
    ql = f"%{query}%"
    try:
        cur = conn.execute("""
            SELECT p.session_id, substr(p.data, 1, 500)
            FROM part p
            WHERE p.data LIKE ?
            AND json_extract(p.data, '$.type') IN ('text', 'tool')
            LIMIT 100
        """, (ql,))
        rows = cur.fetchall()
    except Exception:
        return {}
    finally:
        conn.close()

    result: dict[str, list[str]] = {}
    for sid, data_str in rows:
        try:
            pd = json.loads(data_str)
        except Exception:
            continue
        text = ""
        if pd.get("type") == "text":
            text = pd.get("text", "")
        elif pd.get("type") == "tool":
            text = json.dumps((pd.get("state") or {}).get("input") or {})
        # LIKE ignores case (and reads % and _ as wildcards); a hit is one
        # the transcript search would have made too.
        if not find(text, query):
            continue
        snippets = result.setdefault(sid, [])
        if len(snippets) < 3:
            snippets.append(excerpt(text, query).replace("\n", " "))
    return result


def _tool_calls_by_session(conn: sqlite3.Connection) -> dict[str, list[tuple[str, dict]]]:
    """Every tool call OpenCode recorded, as (tool, input) per session id — one
    query for them all, reading only those two fields of each part (a part
    also holds the call's output)."""
    out: dict[str, list[tuple[str, dict]]] = {}
    for sid, tool, inp in conn.execute("""
            SELECT session_id, json_extract(data, '$.tool'), json_extract(data, '$.state.input')
            FROM part WHERE json_extract(data, '$.type') = 'tool'
    """):
        try:
            inp = json.loads(inp) if inp else {}
        except ValueError:
            continue
        out.setdefault(sid, []).append((tool or "", inp if isinstance(inp, dict) else {}))
    return out


def _activity(calls: list[tuple[str, dict]]) -> dict:
    """Skill/memory activity of a session's tool `calls`, in the shape Claude
    Code's (transcripts.session_activity) and Codex's sessions report it,
    adapted for OpenCode's tool naming: bash, read, write, edit, patch, skill
    (all lowercase), the file path under filePath (not file_path)."""
    skill_invokes: Counter[str] = Counter()
    skill_reads: Counter[str] = Counter()
    skill_writes: Counter[str] = Counter()
    skill_bash: Counter[str] = Counter()
    memory_counts = {"read": Counter(), "write": Counter(), "edit": Counter()}
    memory_ops: list[dict] = []
    mem_seen: set[tuple[str, str]] = set()

    for tool, inp in calls:
        fp = inp.get("filePath", "") or inp.get("file_path", "") or ""
        cmd = inp.get("command", "") or ""

        # Skill formal invocation (opencode has a 'skill' tool)
        if tool == "skill":
            name = inp.get("name", "")
            if name:
                skill_invokes[name] += 1

        # File operations on skill files
        if tool in ("read", "write", "edit", "patch"):
            m = _SKILL_PATH_RE.search(fp)
            if m:
                (skill_reads if tool == "read" else skill_writes)[m.group(1)] += 1

        # File operations on memory files
        if tool in ("read", "write", "edit", "patch") and "/memory/" in fp:
            mem_name = fp.rsplit("/", 1)[-1].replace(".md", "")
            if mem_name == "MEMORY":
                continue
            memory_counts["edit" if tool == "patch" else tool][mem_name] += 1
            key = (mem_name, tool)
            if key not in mem_seen:
                mem_seen.add(key)
                memory_ops.append({"name": mem_name, "operation": tool})

        # Bash referencing skills
        if tool == "bash" and ("skills/" in cmd or "SKILL.md" in cmd):
            for sk in set(_SKILL_PATH_RE.findall(cmd)) or ("_general",):
                skill_bash[sk] += 1

    # Plain dicts: history.HistorySession holds these, and dataclasses.asdict
    # rebuilds a Counter from its (key, count) pairs — counting the pairs.
    return {
        "skills_used": list(set(skill_invokes) | set(skill_reads) | set(skill_writes)),
        "memory_ops": memory_ops,
        "skill_breakdown": {
            "per_skill_invokes": dict(skill_invokes),
            "per_skill_reads": dict(skill_reads),
            "per_skill_writes": dict(skill_writes),
            "per_skill_bash_refs": dict(skill_bash),
        },
        "memory_breakdown": {
            "per_memory_reads": dict(memory_counts["read"]),
            "per_memory_writes": dict(memory_counts["write"]),
            "per_memory_edits": dict(memory_counts["edit"]),
        },
    }


def _tool_preview(inp: dict) -> dict:
    preview: dict = {}
    for k, v in list(inp.items())[:4]:
        if isinstance(v, str):
            preview[k] = cap_text(v, TOOL_ARG_CHARS)
        elif isinstance(v, (int, float, bool)) or v is None:
            preview[k] = v
    return preview


def _ms_to_iso(ms: Optional[int]) -> str:
    if not ms:
        return ""
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).isoformat()

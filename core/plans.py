"""Associate sessions with plan files in ~/.claude/plans/."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .sessions import CLAUDE_HOME

PLANS_DIR = CLAUDE_HOME / "plans"


def list_plans() -> list[dict]:
    if not PLANS_DIR.exists():
        return []
    out: list[dict] = []
    for f in sorted(PLANS_DIR.glob("*.md"), key=lambda p: -p.stat().st_mtime):
        st = f.stat()
        out.append({
            "name": f.stem,
            "path": str(f),
            "mtime": int(st.st_mtime * 1000),
            "size": st.st_size,
        })
    return out


def plan_for_session(
    name_hint: Optional[str],
    cwd: Optional[str],
    transcript_path: Optional[str] = None,
) -> Optional[dict]:
    if not PLANS_DIR.exists():
        return None
    plans = list(PLANS_DIR.glob("*.md"))
    if not plans:
        return None

    # 1. Exact slug match on session name
    if name_hint:
        for f in plans:
            if f.stem == name_hint:
                return _read_plan(f)

    # 2. Extract from transcript: find the last plan file this session wrote/edited
    if transcript_path:
        plan = _plan_from_transcript(transcript_path)
        if plan:
            return plan

    return None


def _plan_from_transcript(transcript_path: str) -> Optional[dict]:
    """Find the last plan file written/edited by this session."""
    p = Path(transcript_path)
    if not p.exists():
        return None
    last_plan_path: Optional[str] = None
    try:
        with p.open() as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "assistant":
                    continue
                for c in (d.get("message") or {}).get("content") or []:
                    if not isinstance(c, dict) or c.get("type") != "tool_use":
                        continue
                    if c.get("name") not in ("Write", "Edit"):
                        continue
                    fp = str((c.get("input") or {}).get("file_path", ""))
                    if "/.claude/plans/" in fp and fp.endswith(".md"):
                        last_plan_path = fp
    except Exception:
        pass
    if last_plan_path:
        pf = Path(last_plan_path)
        if pf.exists():
            return _read_plan(pf)
    return None


def _read_plan(f: Path) -> dict:
    try:
        text = f.read_text(errors="replace")
    except Exception:
        text = ""
    return {
        "name": f.stem,
        "path": str(f),
        "mtime": int(f.stat().st_mtime * 1000),
        "content": text,
    }


def read_plan_by_name(name: str) -> Optional[dict]:
    f = PLANS_DIR / f"{name}.md"
    if not f.exists():
        return None
    return _read_plan(f)

"""Associate sessions with plan files in ~/.claude/plans/."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .sessions import CLAUDE_HOME
from .transcripts import extract_plan_history

PLANS_DIR = CLAUDE_HOME / "plans"


def list_plans() -> list[dict]:
    if not PLANS_DIR.exists():
        return []
    # One stat per file, for both the newest-first order and the row.
    stats = [(f, f.stat()) for f in PLANS_DIR.glob("*.md")]
    stats.sort(key=lambda fs: -fs[1].st_mtime)
    return [{
        "name": f.stem,
        "path": str(f),
        "mtime": int(st.st_mtime * 1000),
        "size": st.st_size,
    } for f, st in stats]


def plan_for_session(
    name_hint: Optional[str],
    transcript_path: Optional[str] = None,
) -> Optional[dict]:
    # 1. Exact slug match on session name
    if name_hint:
        plan = read_plan_by_name(name_hint)
        if plan:
            return plan

    # 2. Extract from transcript: find the last plan file this session wrote/edited
    if transcript_path:
        return _plan_from_transcript(transcript_path)
    return None


def _plan_from_transcript(transcript_path: str) -> Optional[dict]:
    """Find the last plan file written/edited by this session, if it's still
    in the plans directory."""
    history = extract_plan_history(transcript_path)
    if not history:
        return None
    f = PLANS_DIR / history[-1]["plan_file"]
    return _read_plan(f) if f.exists() else None


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
    # A file stem, never a path: session names are free text, and one with a
    # slash in it must not reach outside the plans directory.
    if "/" in name:
        return None
    f = PLANS_DIR / f"{name}.md"
    if not f.exists():
        return None
    return _read_plan(f)

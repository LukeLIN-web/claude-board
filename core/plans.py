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
    # 1. The plan named after the session. A file stem, never a path: session
    # names are free text, and one with a slash in it must not reach outside the
    # plans directory.
    if name_hint and "/" not in name_hint:
        plan = _read_plan(PLANS_DIR / f"{name_hint}.md")
        if plan:
            return plan
    # 2. The last plan file this session wrote or edited, if it's still there.
    history = extract_plan_history(transcript_path) if transcript_path else []
    return _read_plan(PLANS_DIR / history[-1]["plan_file"]) if history else None


def _read_plan(f: Path) -> Optional[dict]:
    if not f.exists():
        return None
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

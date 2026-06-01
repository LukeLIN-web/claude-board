"""Global skill catalog from ~/.claude/skills/ + ~/.codex/skills/ + usage stats."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .sessions import CLAUDE_HOME, HOME_BASE

SKILLS_DIR = CLAUDE_HOME / "skills"
CODEX_SKILLS_DIR = HOME_BASE / ".codex" / "skills"


def _parse_skill_md(path: Path) -> Optional[dict]:
    try:
        text = path.read_text(errors="replace")[:4000]
    except Exception:
        return None
    lines = text.splitlines()
    name = path.parent.name
    description = ""
    trigger = ""
    for line in lines:
        line_s = line.strip()
        if line_s.lower().startswith("# ") and not description:
            description = line_s[2:].strip()
        if "trigger" in line_s.lower() or "use when" in line_s.lower():
            trigger = line_s[:200]
            break
    if not description:
        description = name
    return {
        "name": name,
        "description": description[:200],
        "trigger": trigger[:200],
        "path": str(path),
    }


def list_all_skills() -> list[dict]:
    skills: list[dict] = []
    seen_names: set[str] = set()

    # Claude Code skills
    if SKILLS_DIR.exists():
        for d in sorted(SKILLS_DIR.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            skill_md = d / "SKILL.md"
            if not skill_md.exists():
                continue
            info = _parse_skill_md(skill_md)
            if info:
                info["origin"] = "claude"
                info["is_system"] = False
                skills.append(info)
                seen_names.add(info["name"])

    # Codex skills (user-installed + .system built-ins)
    if CODEX_SKILLS_DIR.exists():
        for d in sorted(CODEX_SKILLS_DIR.iterdir()):
            if not d.is_dir():
                continue
            is_system = d.name == ".system"
            sub_dirs = [d] if not is_system else [x for x in d.iterdir() if x.is_dir()]
            for sd in sub_dirs:
                skill_md = sd / "SKILL.md"
                if not skill_md.exists():
                    continue
                info = _parse_skill_md(skill_md)
                if not info:
                    continue
                if info["name"] in seen_names:
                    # Already covered by Claude side; don't double-list
                    continue
                info["origin"] = "codex-system" if is_system else "codex"
                info["is_system"] = is_system
                skills.append(info)
                seen_names.add(info["name"])

    return skills


def skills_with_usage(usage_counts: dict[str, int]) -> list[dict]:
    """Merge skill catalog with usage counts from session transcripts."""
    skills = list_all_skills()
    for s in skills:
        s["usage_count"] = usage_counts.get(s["name"], 0)
    skills.sort(key=lambda s: (-s["usage_count"], s["name"]))
    return skills

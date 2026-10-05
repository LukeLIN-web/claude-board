"""Global skill catalog from ~/.claude/skills/ + ~/.codex/skills/."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .memory import split_frontmatter
from .sessions import CLAUDE_HOME, HOME_BASE

SKILLS_DIR = CLAUDE_HOME / "skills"
CODEX_SKILLS_DIR = HOME_BASE / ".codex" / "skills"


def _parse_skill_md(path: Path) -> Optional[dict]:
    """A skill's card: its directory name and a one-line description.

    The description is the frontmatter's `description` — the text Claude Code
    itself lists the skill by and matches requests against — else the body's
    first `# ` heading, else the name. This used to skip the frontmatter and
    stop at the first line that said "use when" or "trigger", keeping it as a
    `trigger` nothing read; in most SKILL.md files that line is the
    frontmatter's own `description: Use when…`, which comes before any heading,
    so most skills showed their name a second time where the description goes.
    """
    try:
        text = path.read_text(errors="replace")
    except Exception:
        return None
    fm, body = split_frontmatter(text)
    heading = next((ln.strip()[2:].strip() for ln in body.splitlines()
                    if ln.strip().startswith("# ")), "")
    name = path.parent.name
    description = fm.get("description") or heading or name
    return {"name": name, "description": description[:200], "path": str(path)}


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

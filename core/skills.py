"""Global skill catalog from ~/.claude/skills/ + ~/.codex/skills/."""
from __future__ import annotations

from pathlib import Path
from typing import Iterator, Optional

from .codex import CODEX_HOME
from .memory import split_frontmatter
from .sessions import CLAUDE_HOME

SKILLS_DIR = CLAUDE_HOME / "skills"
CODEX_SKILLS_DIR = CODEX_HOME / "skills"


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


def _skill_dirs() -> Iterator[tuple[Path, str]]:
    """Each directory that may hold a skill, with its origin: Claude Code's,
    then Codex's (user-installed, and the built-ins under `.system`)."""
    if SKILLS_DIR.exists():
        for d in sorted(SKILLS_DIR.iterdir()):
            if d.is_dir() and not d.name.startswith("."):
                yield d, "claude"
    if CODEX_SKILLS_DIR.exists():
        for d in sorted(CODEX_SKILLS_DIR.iterdir()):
            if not d.is_dir():
                continue
            if d.name == ".system":
                yield from ((sd, "codex-system") for sd in d.iterdir() if sd.is_dir())
            else:
                yield d, "codex"


def list_all_skills() -> list[dict]:
    """Every skill once: a name listed already (Claude Code's first) is not
    listed again."""
    skills: list[dict] = []
    seen_names: set[str] = set()
    for d, origin in _skill_dirs():
        skill_md = d / "SKILL.md"
        if d.name in seen_names or not skill_md.exists():
            continue
        info = _parse_skill_md(skill_md)
        if info:
            info["origin"] = origin
            info["is_system"] = origin == "codex-system"
            skills.append(info)
            seen_names.add(d.name)
    return skills

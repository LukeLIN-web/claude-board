"""Parse ~/.claude/projects/*/memory/*.md files with YAML frontmatter."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .sessions import PROJECTS_DIR


def _parse_frontmatter(text: str) -> dict:
    """Minimal YAML frontmatter parser for memory files."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    block = text[4:end]
    result: dict = {}
    for line in block.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if key == "metadata":
                continue
            if key == "type" and not result.get("type"):
                result["type"] = val
            else:
                result[key] = val
    return result


def list_memories(project_slug: Optional[str] = None) -> dict:
    """Return memories grouped by type for a project."""
    if not PROJECTS_DIR.exists():
        return {"groups": {}, "total": 0}

    if project_slug:
        dirs = [PROJECTS_DIR / project_slug / "memory"]
    else:
        dirs = []
        for d in PROJECTS_DIR.iterdir():
            mem_dir = d / "memory"
            if mem_dir.is_dir():
                dirs.append(mem_dir)

    memories: list[dict] = []
    for mem_dir in dirs:
        if not mem_dir.exists():
            continue
        # Parse MEMORY.md index: any memory referenced there is implicitly
        # loaded into every session's system prompt (so explicit-read count
        # alone is misleading).
        index_names = _parse_memory_index(mem_dir / "MEMORY.md")
        for f in sorted(mem_dir.glob("*.md")):
            if f.name == "MEMORY.md":
                continue
            try:
                text = f.read_text(errors="replace")
            except Exception:
                continue
            fm = _parse_frontmatter(text)
            body_start = text.find("\n---", 3)
            body = text[body_start + 4:].strip() if body_start > 0 else text
            memories.append({
                "name": fm.get("name", f.stem),
                "file_stem": f.stem,
                "description": fm.get("description", ""),
                "type": fm.get("type", "unknown"),
                "path": str(f),
                "content_preview": body[:500],
                "project_slug": mem_dir.parent.name,
                "in_memory_index": f.stem in index_names,
            })

    groups: dict[str, list[dict]] = {}
    for m in memories:
        t = m["type"]
        groups.setdefault(t, []).append(m)

    return {"groups": groups, "total": len(memories)}


def _parse_memory_index(index_path) -> set:
    """Parse MEMORY.md, extract memory file stems mentioned in it.
    Any memory in this index is implicitly loaded into every session's
    system prompt by the harness.
    """
    import re
    names: set[str] = set()
    try:
        text = index_path.read_text(errors="replace")
    except Exception:
        return names
    # Match patterns like `memory/foo.md`, `(foo.md)`, `[foo](foo.md)`, etc
    for m in re.finditer(r'([A-Za-z0-9_-]+)\.md', text):
        names.add(m.group(1))
    return names

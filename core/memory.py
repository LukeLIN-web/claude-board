"""Parse ~/.claude/projects/*/memory/*.md files with YAML frontmatter.

split_frontmatter is also how core/skills.py reads a SKILL.md, which opens with
the same kind of block."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .sessions import PROJECTS_DIR


# Frontmatter is a `---` line that opens the file, the fields, and the next line
# that is `---` alone. Only a fence on the very first line opens it: further
# down, `---` is a markdown horizontal rule, and a file that starts with
# anything else has no frontmatter however many rules its body draws. `\r?`
# because a file saved on Windows ends its lines in CRLF.
_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)^---[ \t]*\r?$", re.S | re.M)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _entries(block: str) -> list[tuple[str, str, list[str]]]:
    """(key, value, lines under it) for each top-level `key: value` line of a
    frontmatter block. The lines under a key are the indented and blank ones
    that follow it, up to the next top-level key."""
    entries: list[tuple[str, str, list[str]]] = []
    for line in block.splitlines():
        if entries and (not line.strip() or _indent(line)):
            entries[-1][2].append(line)
            continue
        key, sep, value = line.partition(":")
        if sep:
            entries.append((key.strip(), value.strip(), []))
    return entries


# The first line under a key whose value is empty, when it makes that key a list
# (`- item`) or a mapping (`child: …`) rather than text continued from the key.
_CONTAINER_LINE = re.compile(r"(-|[\w-]+:)(\s|$)")
_ESCAPES = {"n": "\n", "t": "\t"}


def _value(value: str, lines: list[str]) -> str:
    """A field's text, read the way YAML reads the shapes these files use.

    A SKILL.md writes its `description` plain, in double quotes with `\\"`
    escapes (a memory too, often), folded over several lines with `>-`, or
    quoted on the lines below an empty `description:`. So: a `>` block is
    folded into one line and a `|` block keeps its lines; a plain or quoted
    value continues on the indented lines below its key, joined by spaces, as
    YAML folds them; then the quotes and escapes come off. A key whose lines are
    a list or a mapping has no text value. (A ` #` is kept, not read as a
    comment: memory descriptions write `#123` and mean it as text.)
    """
    rest = [ln.strip() for ln in lines]
    if value[:1] == "|":
        return "\n".join(rest).strip()
    if value[:1] == ">":
        return " ".join(ln for ln in rest if ln)
    if not value and _CONTAINER_LINE.match(next((ln for ln in rest if ln), "")):
        return ""
    text = " ".join([value, *(ln for ln in rest if ln)]).strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return re.sub(r"\\(.)", lambda m: _ESCAPES.get(m[1], m[1]), text[1:-1])
    if len(text) >= 2 and text[0] == text[-1] == "'":
        return text[1:-1].replace("''", "'")
    return text


def _parse_frontmatter(block: str) -> dict:
    """The fields of a frontmatter block, as {key: value}.

    Claude Code has written a memory's fields in two shapes: all at the top
    level (`type: user`), and, in newer files, `name` and `description` at the
    top with `type`, `node_type`, `originSessionId` … indented under
    `metadata:`. A field under `metadata:` stands in for a top-level one the
    file lacks and never replaces one it has, whichever comes first; keys
    indented under any other parent, or deeper under `metadata`, are not fields
    of the file at all. (Every key used to lose its indent, so a `metadata.type`
    written after a top-level `type` overwrote it and the memory was filed
    under the wrong group.)
    """
    top: dict = {}
    under_metadata: dict = {}
    for key, value, lines in _entries(block):
        if key != "metadata" or value:
            top[key] = _value(value, lines)
            continue
        depth = min((_indent(ln) for ln in lines if ln.strip()), default=0)
        for ln in lines:
            k, sep, v = ln.partition(":")
            if sep and ln.strip() and _indent(ln) == depth:
                under_metadata[k.strip()] = _value(v.strip(), [])
    return {**under_metadata, **top}


def split_frontmatter(text: str) -> tuple[dict, str]:
    """(frontmatter fields, body) of a memory file or a SKILL.md. The body is
    what follows the closing `---`; a file that doesn't open with a frontmatter
    block is all body (it used to be cut at its first horizontal rule, as if
    that closed one)."""
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text
    return _parse_frontmatter(m.group(1)), text[m.end():].strip()


def find_memory(name: str) -> Optional[Path]:
    """The memory file `name` (its file stem), in the first project that has one."""
    for proj_dir in PROJECTS_DIR.iterdir():
        f = proj_dir / "memory" / f"{name}.md"
        if f.exists():
            return f
    return None


def memory_detail(name: str) -> Optional[dict]:
    """The memory file `name` (see find_memory), read whole, or None."""
    f = find_memory(name)
    if not f:
        return None
    fm, body = split_frontmatter(f.read_text(errors="replace"))
    return {
        "name": fm.get("name", name),
        "description": fm.get("description", ""),
        "type": fm.get("type", "unknown"),
        "content": body,
        "path": str(f),
    }


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
            fm, body = split_frontmatter(text)
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
    names: set[str] = set()
    try:
        text = index_path.read_text(errors="replace")
    except Exception:
        return names
    # Match patterns like `memory/foo.md`, `(foo.md)`, `[foo](foo.md)`, etc
    for m in re.finditer(r'([A-Za-z0-9_-]+)\.md', text):
        names.add(m.group(1))
    return names

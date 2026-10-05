"""Builders more than one test module needs: scratch files, transcript rows, windows.

The transcript rows are the shapes Claude Code writes, cut to the fields the
board reads. A typed prompt is logged with its content as a plain string, and as
a list of content blocks once it carries anything but text (or on some CLI
versions always) — both are read, so `user_row` builds either.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from core import sessions

TS = "2026-08-10T21:26:21Z"


def scratch_dir() -> Path:
    """A new, empty directory for one test's files.

    Under pytest it is made inside the run's temporary home (tests/conftest.py),
    which is removed when the run ends, so a test needs no cleanup of its own.
    """
    return Path(tempfile.mkdtemp(dir=os.environ.get("CLAUDE_BOARD_TEST_HOME")))


def write_jsonl(rows, path=None, *, ensure_ascii=True) -> Path:
    """Write `rows` one JSON object per line and return the file.

    Without `path` the file is a fresh t.jsonl of its own: a new path on every
    call, because transcripts are memoized per file. `ensure_ascii=False` writes
    non-ASCII text as UTF-8 the way the CLIs do, instead of as \\u escapes.
    """
    p = Path(path) if path is not None else scratch_dir() / "t.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r, ensure_ascii=ensure_ascii) + "\n" for r in rows),
                 encoding="utf-8")
    return p


def user_row(text, ts=TS, *, blocks=False, **row) -> dict:
    """A `user` row: `text` as a plain string, or as one text block with
    `blocks=True`. Extra keyword fields go on the row (isMeta, promptSource, …)."""
    content = [{"type": "text", "text": text}] if blocks else text
    return {"type": "user", "timestamp": ts,
            "message": {"role": "user", "content": content}, **row}


def assistant_row(text, ts=TS, *, model="claude-opus-5") -> dict:
    """An `assistant` row that answered with one text block."""
    return {"type": "assistant", "timestamp": ts,
            "message": {"model": model, "content": [{"type": "text", "text": text}]}}


def queue_op(op, content=None, ts=TS) -> dict:
    """A `queue-operation` row. `dequeue` carries no content, so None leaves it out."""
    row = {"type": "queue-operation", "operation": op, "timestamp": ts}
    if content is not None:
        row["content"] = content
    return row


def codex_rollout(root, sid, cwd, rows=(), *, page="", time="2026-10-05T08-00-00") -> Path:
    """A Codex rollout under `root`, named and opened the way Codex writes one:
    `<y>/<m>/<d>/rollout-<time>-<sid>.jsonl` — `_<page>` after the id for a
    further page of a paginated thread — whose first line is the session_meta
    recording `cwd`, followed by `rows`."""
    day = Path(root, *time[:10].split("-"))
    name = f"rollout-{time}-{sid}" + (f"_{page}" if page else "")
    meta = {"type": "session_meta", "payload": {"id": sid, "cwd": cwd, "timestamp": TS}}
    return write_jsonl([meta, *rows], day / f"{name}.jsonl")


def make_window(**over) -> sessions.Window:
    """A live, visible Claude window on /dev/pts/3; override any field."""
    fields = dict(pid=100, session_id="", cwd="/tmp/proj", project_name="proj",
                  project_slug="-tmp-proj", name=None, status="idle", waiting_for=None,
                  started_at=0, updated_at=0, version="", tty="/dev/pts/3",
                  transcript_path=None, alive=True, hidden=False, platform="claude")
    fields.update(over)
    return sessions.Window(**fields)

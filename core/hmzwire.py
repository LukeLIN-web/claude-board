"""Calls to this machine's hmz daemon, over its Unix socket.

A run's record on disk (see hmz.py) says what began, which sessions opened and how it
ended; what the run is waiting on is held by the daemon alone. Since hmz 0.3.0 a flow can
give a role to the person (an `Outworlder`) and stop until somebody answers, and nothing of
that question is written to disk: the daemon's `hosts.status` has it, as `pending`, and its
notices say when a run asked, ended, failed or was stopped.

The wire is hmz's own (its docs, reference/daemon): every frame a big-endian u32 length and
one JSON-RPC 2.0 object, and a session opens with `session.hello`. The socket is
`humanize-<uid>/daemon.sock` in the system temp directory, and connecting to it starts no
daemon: with none running there is no run, and every call here answers None.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import tempfile
from typing import Optional

PROTOCOL = "3.0.0"
TIMEOUT = 2.0


def sock_path() -> str:
    return os.path.join(tempfile.gettempdir(), f"humanize-{os.getuid()}", "daemon.sock")


def _recv(s: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        part = s.recv(n - len(buf))
        if not part:
            raise ConnectionError("the daemon closed the session")
        buf += part
    return buf


def _frame(s: socket.socket, obj: dict) -> None:
    data = json.dumps(obj).encode()
    s.sendall(struct.pack(">I", len(data)) + data)


def _answer(s: socket.socket, want: int) -> dict:
    while True:  # notifications may come between; the answer is the one with our id
        msg = json.loads(_recv(s, struct.unpack(">I", _recv(s, 4))[0]))
        if msg.get("id") == want:
            if "error" in msg:
                raise RuntimeError(msg["error"].get("message", "hmz daemon error"))
            return msg.get("result") or {}


def calls(*requests: tuple[str, dict], path: Optional[str] = None) -> Optional[list[dict]]:
    """Answers to `requests` ((method, params) each) in one session, or None when no
    daemon answers here or it refuses the session."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(TIMEOUT)
            s.connect(path or sock_path())
            _frame(s, {"jsonrpc": "2.0", "id": 0, "method": "session.hello",
                       "params": {"protocol": PROTOCOL, "client": {"name": "claude-board", "kind": "bridge"}}})
            _answer(s, 0)
            out = []
            for i, (method, params) in enumerate(requests, 1):
                _frame(s, {"jsonrpc": "2.0", "id": i, "method": method, "params": params})
                out.append(_answer(s, i))
            return out
    except (OSError, ValueError, RuntimeError, ConnectionError):
        return None


def pending(workspace: str) -> Optional[list[dict]]:
    """The questions the run in `workspace` (the workspace's own host) asked and nobody has
    answered yet, oldest first; None when that cannot be told."""
    got = calls(("hosts.status", {"workspace": workspace}))
    return None if got is None else list(got[0].get("pending") or [])


def notices(after: int = 0) -> Optional[tuple[list[dict], int]]:
    """The daemon's notices after id `after` (done, failed, stopped, asked, listening), oldest
    first, and the newest id; None when no daemon answers."""
    got = calls(("notices.list", {"after": after}))
    return None if got is None else (list(got[0].get("notices") or []), int(got[0].get("last") or 0))

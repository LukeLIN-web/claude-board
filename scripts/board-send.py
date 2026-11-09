#!/usr/bin/env python3
"""board-send.py — type a prompt into a card by name, from any host's shell.

    python3 scripts/board-send.py <target> <text…>
    echo "<text>" | python3 scripts/board-send.py <target> -

<target> is matched against every card this host's board shows — with
FLEET_UPSTREAM set that is the whole fleet (core/peers.py UPSTREAM) — as, in
order: the card key ("63:124275"), the session name ("agent-harness-dispatcher"),
or a prefix of at least 6 characters of the session id ("79d5d1aa"). The first
rule that matches anything decides; more than one card under it is an error
that lists them, never a guess.

The send goes through the board's own prompt route, so it gets what that route
does — the right tmux server and pane, the landed check, the paste handling for
long text, the forward to the host that owns the card — rather than a
hand-rolled `tmux send-keys`. Newlines are folded into spaces there.

Exit: 0 sent · 1 the board refused or failed the send · 2 no card / ambiguous / usage
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIN_ID_PREFIX = 6


def board() -> tuple[str, str]:
    """This host's board URL and API token, from the same loader the board uses
    — or CLAUDE_BOARD_URL / CLAUDE_BOARD_TOKEN, as dispatch.py takes them."""
    port, token = subprocess.run(
        ["bash", "-c", 'source scripts/env.sh >/dev/null 2>&1; printf "%s\\0%s" "$PORT" "${FLEET_API_TOKEN-}"'],
        cwd=ROOT, capture_output=True, text=True, check=True).stdout.split("\0")
    return (os.environ.get("CLAUDE_BOARD_URL") or f"http://127.0.0.1:{port}",
            os.environ.get("CLAUDE_BOARD_TOKEN") or token)


def call(url: str, token: str, method: str = "GET", body: dict | None = None, timeout: float = 120):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return json.loads(raw)
        except ValueError:
            return {"ok": False, "error": f"HTTP {e.code}: {raw[:300]}"}


def match(windows: list[dict], target: str) -> list[dict]:
    rules = [
        lambda w: str(w.get("key")) == target,
        lambda w: w.get("name") == target,
        lambda w: len(target) >= MIN_ID_PREFIX and str(w.get("session_id") or "").startswith(target),
    ]
    for rule in rules:
        hits = [w for w in windows if rule(w)]
        if hits:
            return hits
    return []


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    target = argv[1]
    text = sys.stdin.read() if argv[2:] == ["-"] else " ".join(argv[2:])
    if not text.strip():
        print("board-send: empty prompt", file=sys.stderr)
        return 2
    base, token = board()
    snap = call(f"{base}/api/windows", token, timeout=30)
    if not isinstance(snap, dict) or "windows" not in snap:
        print(f"board-send: board at {base} answered {str(snap)[:200]}", file=sys.stderr)
        return 2
    hits = match(snap["windows"], target)
    if len(hits) != 1:
        why = "no card matches" if not hits else "more than one card matches"
        print(f"board-send: {why} {target!r}", file=sys.stderr)
        for w in hits:
            print(f"  {w.get('key')}  {w.get('name')}  {w.get('session_id')}  {w.get('cwd')}", file=sys.stderr)
        if not hits and not (snap.get("peers") or []):
            hint = ", ".join(snap.get("polled_by") or [])
            print("  this board shows only its own host"
                  + (f"; the fleet view is on {hint} — set FLEET_UPSTREAM to it" if hint else ""),
                  file=sys.stderr)
        return 2
    w = hits[0]
    key = urllib.parse.quote(str(w["key"]), safe="")
    r = call(f"{base}/api/windows/{key}/prompt", token, "POST", {"text": text})
    ok = isinstance(r, dict) and r.get("ok")
    print(f"board-send: {'sent to' if ok else 'FAILED for'} {w.get('key')} ({w.get('name')})"
          + ("" if ok else f": {(r or {}).get('error') if isinstance(r, dict) else r}"),
          file=sys.stderr if not ok else sys.stdout)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

"""Failure episodes AgentShark found in this host's sessions, as a badge on
each card. AgentShark (github.com/naisslab/AgentShark) is an optional, local,
read-only indexer of Claude Code and Codex logs. It finds what triage can't
see from a card's state: a run that "completed" on an unrecovered error, a
subagent that failed silently, a call blocked by policy or the sandbox.

NOT A SECOND AUTHORITY: AgentShark owns these facts and the board only shows
them. The badge is its own card field (`agentshark`) and never feeds triage.

OPTIONAL: with no `agentshark` on the board's PATH the sampler never starts,
and cards carry no `agentshark` field, so the board is exactly as without it.

KEEPING THE INDEX CURRENT: `agentshark failures` reads the index and doesn't
update it. Only `agentshark serve` keeps it current on its own, and the board
can't assume one is running. So every sample first runs `agentshark index`
(incremental, under a second). It writes only AgentShark's own index, never
~/.claude or ~/.codex. When another AgentShark process already holds the index
lock, that run fails and the read below still gets the index that process is
bringing up to date.

WHICH EPISODES: what `agentshark failures` lists by default (blocked and
unrecovered, not recovered ones) with evidence in the last WINDOW, matched to a
card by session id. A subagent's episodes (`cc:<root>/<agent>`) count on its
root session's card.

NEVER BLOCKS: a thread samples every SAMPLE_INTERVAL and the snapshot reads the
last good sample, the same rule as core/gpus.py. Each host reads its own index.
The field rides the card to an aggregating board like any other card field.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from typing import Optional

SAMPLE_INTERVAL = 60.0
WINDOW = "1d"
TIMEOUT = 60.0
SHOWN = 5  # episodes listed on a card, newest first; `count` has them all

# session uuid -> the card field; replaced whole, never edited, so readers need no lock.
_latest: dict[str, dict] = {}
_thread: Optional[threading.Thread] = None


def by_session(report: dict) -> dict[str, dict]:
    """`agentshark failures --json` output -> the card field for each session uuid."""
    groups: dict[str, list[dict]] = {}
    for e in report.get("episodes") or []:
        root, _, sub = e["session_id"].partition("/")
        groups.setdefault(root, []).append(dict(e, subagent=sub or None))
    out = {}
    for root, eps in groups.items():
        eps.sort(key=lambda e: -e["ts"])
        sections: dict[str, int] = {}
        for e in eps:
            sections[e["section"]] = sections.get(e["section"], 0) + 1
        out[root.partition(":")[2]] = {
            "count": len(eps),
            "sections": sections,
            "live": any(e.get("live") for e in eps),
            "episodes": [{"ts": e["ts"] // 1_000_000, "section": e["section"], "outcome": e.get("outcome"),
                          "family": e.get("family"), "signature": e.get("signature_excerpt") or "",
                          "target": e.get("target_excerpt") or "", "subagent": e["subagent"],
                          "live": bool(e.get("live"))}
                         for e in eps[:SHOWN]],
            "command": f"agentshark why --session {root}",
        }
    return out


def sample(exe: str) -> dict[str, dict]:
    subprocess.run([exe, "index"], capture_output=True, timeout=TIMEOUT)  # lock held elsewhere: read as is
    out = subprocess.run([exe, "failures", "--since", WINDOW, "--json"],
                         capture_output=True, text=True, check=True, timeout=TIMEOUT).stdout
    return by_session(json.loads(out))


def _sample_loop(exe: str) -> None:
    global _latest
    while True:
        try:
            _latest = sample(exe)
        except Exception as e:
            print(f"[agentshark] sample error: {e}")
        time.sleep(SAMPLE_INTERVAL)


def start() -> None:
    """Start the sampler thread if agentshark is installed. Idempotent."""
    global _thread
    exe = shutil.which("agentshark")
    if not exe or (_thread and _thread.is_alive()):
        return
    _thread = threading.Thread(target=_sample_loop, args=(exe,), name="fleet-agentshark", daemon=True)
    _thread.start()


def attach(windows: list[dict]) -> None:
    """Put the badge on each of this host's cards whose session has episodes."""
    latest = _latest
    for w in windows:
        hit = latest.get(w.get("session_id") or "")
        if hit:
            w["agentshark"] = hit

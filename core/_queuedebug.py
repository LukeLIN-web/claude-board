"""TEMPORARY diagnostic for the "prompt was sent but card still shows queued"
bug. Delete this module (and its call site in app.py) once the root cause is
found. It logs, whenever a card shows queued items, WHY each dashboard-tracked
item failed to reconcile — distinguishing:

  - NOT-IN-TRANSCRIPT : text never appears (swallowed / never submitted)
  - OUT-OF-WINDOW     : text is in the full transcript but not in the last-100-
                        line reconcile window (window too small)
  - TS-GUARDED        : text is in the window but every hit has ts < send ts
  - source=tui        : the item came from the pane scrape, not the tracker

Throttled to once per ~8s per pid so it can't flood.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import promptqueue, transcripts

_LOG = Path(__file__).resolve().parent.parent / "queue_debug.log"
_last: dict[int, float] = {}


def _all_user_norms(tp: str) -> list[tuple[float, str]]:
    """(ts, norm) for EVERY genuine user_text in the full transcript."""
    out: list[tuple[float, str]] = []
    p = Path(tp)
    if not p.exists():
        return out
    with p.open() as f:
        for ln in f:
            try:
                d = json.loads(ln)
            except Exception:
                continue
            if d.get("type") != "user":
                continue
            for ev in transcripts._normalize(d):
                if ev.kind == "user_text" and ev.text.strip():
                    out.append((transcripts._parse_ts(ev.ts) or 0.0,
                                promptqueue.norm(ev.text)))
    return out


def dump(pid: int, status: str, tp: str | None, queued: list[dict]) -> None:
    if not queued:
        return
    now = time.time()
    if now - _last.get(pid, 0.0) < 8.0:
        return
    _last[pid] = now

    lines = [f"\n===== {time.strftime('%H:%M:%S')} pid={pid} status={status} "
             f"queued={len(queued)} ====="]
    window = transcripts.recent_user_texts(tp) if tp else []
    win_norms = [(ts, promptqueue.norm(txt)) for ts, txt in window]
    full = _all_user_norms(tp) if tp else []
    lines.append(f"  reconcile-window rows={len(window)}  full-transcript "
                 f"user-rows={len(full)}")

    # Raw tracked items still pending (with their send ts), keyed by norm.
    raw = {it["norm"]: it for it in (promptqueue._sent.get(pid) or [])}
    for q in queued:
        src = q.get("source")
        txt = q.get("text") or ""
        n = promptqueue.norm(txt)
        it = raw.get(n)
        send_ts = it["ts"] if it else None
        in_win = [ts for ts, wn in win_norms if wn == n]
        in_full = [ts for ts, fn in full if fn == n]
        if src == "tui":
            verdict = "source=tui (pane scrape)"
        elif not in_full:
            verdict = "NOT-IN-TRANSCRIPT (swallowed/never submitted)"
        elif not in_win:
            verdict = (f"OUT-OF-WINDOW (in full transcript x{len(in_full)}, "
                       f"not in last-100-line window)")
        elif send_ts is not None and in_win and max(in_win) < send_ts:
            verdict = (f"TS-GUARDED (window hit ts={max(in_win):.1f} < "
                       f"send ts={send_ts:.1f})")
        else:
            verdict = ("UNEXPECTED: in window with ts>=send but still queued — "
                       "match logic bug?")
        age = f"{now - send_ts:.0f}s" if send_ts else "?"
        lines.append(f"  [{src}] age={age} {verdict}  text={txt[:70]!r}")

    try:
        with _LOG.open("a") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass

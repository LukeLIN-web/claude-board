"""Fold other hosts' boards into this one, so one page shows every machine.

WHY A PEER BOARD AND NOT A SECOND HOME DIR: the obvious way to show another
machine's sessions is to point the reader at its `~/.claude` over the shared
NFS mount. That reads fine and then fails at everything the board is for. A
card is alive because `/proc/<pid>` exists *on this host*, so a remote pid is
either missing or — worse — a live local process that has nothing to do with
it; the UI keys cards by pid, so two hosts collide; and every action
(send_prompt, spawn, close, focus) is a tmux call on the local machine. The
sessions would render and none of them would be real.

So each host runs its own board over its own `~/.claude` and its own tmux, and
one of them aggregates: it polls its peers' `/api/windows` and merges their
cards in, then forwards per-card actions back to the board that owns them.
Every action still executes on the machine whose pid it names.

ADDRESSING: a card is identified by `key`, not pid — `"12345"` for a local
session, `"b:12345"` for one on the peer labelled b. `split_key` is the only
place that mapping is decoded, and routes use it to decide local-or-forward.

CONFIG (per host, in `.env.local.<hostname>` so the shared `.env.local` doesn't
hand every host the same peer list):
  FLEET_PEERS=b=http://127.0.0.1:7880,c=http://127.0.0.1:7881
The label is that machine's short name on the board (CLAUDE_FLEET_LABEL, else
the last octet of its IP). The URL is normally a loopback port forwarded over
ssh (scripts/peer-tunnel.sh) rather than the peer's LAN address — the peer's
board stays bound to 127.0.0.1 and nothing new is exposed on the network.
FLEET_API_TOKEN (shared .env.local) authenticates the hop; without it a gated
peer answers 401.

STALENESS: peer windows come from the last successful poll, never from a
blocking fetch in the request path — a wedged peer must not stall the local
board. Cards older than STALE_AFTER are marked (the UI dims them) and dropped
entirely after DROP_AFTER, so a board that went away stops showing ghosts.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Optional

# Peer poll cadence — matches the local watcher's 2s tick, so a peer card is at
# most one tick behind a local one.
POLL_INTERVAL = 2.0
POLL_TIMEOUT = 4.0
# A GET is a read; a POST types into a pane and waits for the keystrokes to land
# (see actions.send_prompt), which is seconds, not milliseconds.
GET_TIMEOUT = 20.0
POST_TIMEOUT = 90.0
STALE_AFTER = 10.0
DROP_AFTER = 120.0

# Header that marks a request as board-to-board. The peer answers it with its
# own local windows only — without it, two boards pointed at each other would
# each try to render the other's rendering of itself.
PEER_HEADER = "X-Fleet-Peer"


def host_ip_octet() -> str:
    """Last octet of this machine's primary outbound IP (e.g. "12" for
    10.0.0.12), so a board self-identifies by host without manual config.
    Empty string if the IP can't be determined."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No packets are sent; this just picks the interface the kernel would
        # use to reach an external address.
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()
    return ip.rsplit(".", 1)[-1] if ip else ""


def local_label() -> str:
    """This host's label, shown on its cards and used as the local half of a
    card key. CLAUDE_FLEET_LABEL wins; otherwise the IP's last octet."""
    return os.environ.get("CLAUDE_FLEET_LABEL", "").strip() or host_ip_octet()


def _parse_peers(raw: str) -> dict[str, str]:
    """`"b=http://127.0.0.1:7880, c=http://…"` → {label: base_url}.

    Malformed entries are skipped rather than raising: a typo in a per-host env
    file should cost one peer, not the whole board."""
    out: dict[str, str] = {}
    for chunk in raw.replace(",", " ").split():
        label, sep, url = chunk.partition("=")
        label, url = label.strip(), url.strip().rstrip("/")
        if not sep or not label or not url.startswith(("http://", "https://")):
            continue
        if ":" in label:  # ':' separates label from pid in a card key
            continue
        out[label] = url
    return out


_peers: dict[str, str] = {}
_cache: dict[str, dict] = {}
_lock = threading.Lock()
_threads: dict[str, threading.Thread] = {}


def reload_config() -> dict[str, str]:
    """(Re)read FLEET_PEERS. Called at import; exposed for tests."""
    global _peers
    _peers = _parse_peers(os.environ.get("FLEET_PEERS", ""))
    return _peers


reload_config()


def configured() -> dict[str, str]:
    return dict(_peers)


def enabled() -> bool:
    return bool(_peers)


def split_key(key: str) -> tuple[Optional[str], int]:
    """`"b:1234"` → ("b", 1234); `"1234"` → (None, 1234).

    A label that isn't a configured peer resolves to local — including this
    host's own label, so a key minted here round-trips. Raises ValueError if
    the pid half isn't an integer, which routes turn into a 404."""
    label, sep, rest = str(key).partition(":")
    if not sep:
        return None, int(label)
    if label in _peers:
        return label, int(rest)
    return None, int(rest)


def _request(url: str, method: str, body: Optional[dict], timeout: float) -> tuple[int, Any]:
    """One board-to-board HTTP call. Returns (status, parsed-json).

    An HTTP error carrying a JSON body is returned, not raised: a peer's 404
    for an unknown pid is a real answer and should reach the caller as one."""
    data = None
    headers = {PEER_HEADER: "1"}
    token = (os.environ.get("FLEET_API_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"detail": raw[:500] or e.reason}


def _stamp(win: dict, label: str) -> dict:
    """Re-address one peer window as this board sees it."""
    win["host"] = label
    win["key"] = f"{label}:{win.get('pid')}"
    return win


def _poll_once(label: str, base: str) -> None:
    try:
        status, payload = _request(f"{base}/api/windows", "GET", None, POLL_TIMEOUT)
    except Exception as e:  # urllib raises URLError, socket.timeout, ssl…
        _record_error(label, f"{type(e).__name__}: {e}")
        return
    if status != 200 or not isinstance(payload, dict):
        _record_error(label, f"HTTP {status}")
        return
    wins = [_stamp(w, label) for w in payload.get("windows", []) if isinstance(w, dict)]
    with _lock:
        _cache[label] = {
            "windows": wins,
            "ts": time.time(),
            "error": None,
            "tmux_available": bool(payload.get("tmux_available")),
        }


def _record_error(label: str, msg: str) -> None:
    """Keep the last good windows, only note the failure — a peer that blips
    between polls shouldn't blank its cards. Age is what eventually drops them."""
    with _lock:
        entry = _cache.setdefault(label, {"windows": [], "ts": 0.0, "tmux_available": False})
        entry["error"] = msg


def _poll_loop(label: str, base: str) -> None:
    while True:
        try:
            _poll_once(label, base)
        except Exception as e:
            _record_error(label, f"{type(e).__name__}: {e}")
        time.sleep(POLL_INTERVAL)


def start() -> None:
    """Start one polling thread per configured peer. Idempotent."""
    for label, base in _peers.items():
        t = _threads.get(label)
        if t and t.is_alive():
            continue
        t = threading.Thread(target=_poll_loop, args=(label, base),
                             name=f"fleet-peer-{label}", daemon=True)
        _threads[label] = t
        t.start()


def remote_windows() -> list[dict]:
    """Every peer's cards, from the last successful poll. Never blocks."""
    now = time.time()
    out: list[dict] = []
    with _lock:
        entries = {k: dict(v) for k, v in _cache.items()}
    for label, entry in entries.items():
        if label not in _peers:
            continue  # peer removed from the config since it was polled
        age = now - (entry.get("ts") or 0)
        if age > DROP_AFTER:
            continue
        stale = age > STALE_AFTER
        for w in entry.get("windows", []):
            w = dict(w)
            w["peer_stale"] = stale
            out.append(w)
    return out


def status() -> list[dict]:
    """Per-peer health for the UI header: online, how stale, last error."""
    now = time.time()
    with _lock:
        entries = {k: dict(v) for k, v in _cache.items()}
    out = []
    for label in sorted(_peers):
        entry = entries.get(label) or {}
        ts = entry.get("ts") or 0
        age = (now - ts) if ts else None
        out.append({
            "host": label,
            "url": _peers[label],
            "online": bool(ts) and age is not None and age <= STALE_AFTER,
            "age_seconds": round(age, 1) if age is not None else None,
            "windows": len(entry.get("windows", [])),
            "error": entry.get("error"),
        })
    return out


def forward(label: str, method: str, path: str, body: Optional[dict] = None) -> Any:
    """Run one request on the peer that owns the card and return its answer.

    Errors are surfaced as a normal action result (`{"ok": false, "error": …}`)
    rather than an exception: the buttons on a card all render `error`, so an
    unreachable peer reads as "couldn't reach host b" on the card instead of a
    stack trace in the log and a silent button."""
    base = _peers.get(label)
    if not base:
        return {"ok": False, "error": f"unknown peer host {label!r}"}
    timeout = POST_TIMEOUT if method.upper() == "POST" else GET_TIMEOUT
    try:
        status_code, payload = _request(f"{base}{path}", method.upper(), body, timeout)
    except Exception as e:
        return {"ok": False, "error": f"could not reach host {label}: {type(e).__name__}"}
    if status_code >= 400:
        detail = payload.get("detail") if isinstance(payload, dict) else None
        return {"ok": False, "error": f"host {label}: {detail or f'HTTP {status_code}'}",
                "status": status_code}
    return payload

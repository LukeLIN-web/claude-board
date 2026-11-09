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
cards (and GPU rows, core/gpus.py) in, then forwards per-card actions back to
the board that owns them.
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

UPSTREAM: peers are read one way — the aggregator polls its peers, and a peer
sees only itself. A board that sets FLEET_UPSTREAM=<aggregator's url> instead
reads the aggregator's whole fleet (VIEW_HEADER) and shows it beside its own
cards, so a session on any host can find and drive any card. Actions on those
cards go back through the aggregator, key unchanged ("63:1234"): it owns the
route to 63. The aggregator answers the view with its local cards and its
direct peers' only, never its own upstream's, so no chain of boards can loop;
and a host this board reaches directly (its own label, a FLEET_PEERS entry)
never routes through the upstream.

POLLED BY: every board-to-board request names its sender (FROM_HEADER). A board
remembers who polled it lately, so a host that is only someone's peer can say
"the fleet view is on 71" instead of looking like the whole fleet.

STALENESS: peer windows come from the last successful poll, never from a
blocking fetch in the request path — a wedged peer must not stall the local
board. Cards older than STALE_AFTER are marked (the UI dims them) and dropped
entirely after DROP_AFTER, so a board that went away stops showing ghosts.
A card's age for the mark runs from when the peer built it — its answer says
how long before it answered (`age`) — not from when the answer arrived: a peer
stuck mid-refresh answers every poll at once, with the same cards for minutes.
Dropping runs from the last answer, so that peer's cards stay up, dimmed: they
are real sessions, only old.
"""
from __future__ import annotations

import copy
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
# The sender's label, on every board-to-board request (see POLLED BY).
FROM_HEADER = "X-Fleet-From"
# Set with PEER_HEADER by a board reading its upstream: answer with the fleet
# (local cards plus direct peers'), every card keyed "<host>:<pid>".
VIEW_HEADER = "X-Fleet-View"
# Cache key of the upstream's entry. ':' can't appear in a peer label.
_UPSTREAM = ":upstream"
# How long a poller counts as polling this board (see POLLED BY).
POLLED_FOR = 30.0


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
_upstream = ""
_polled_by: dict[str, float] = {}
_cache: dict[str, dict] = {}
_lock = threading.Lock()
_threads: dict[str, threading.Thread] = {}


def reload_config() -> dict[str, str]:
    """(Re)read FLEET_PEERS and FLEET_UPSTREAM. Called at import; exposed for tests."""
    global _peers, _upstream
    _peers = _parse_peers(os.environ.get("FLEET_PEERS", ""))
    up = os.environ.get("FLEET_UPSTREAM", "").strip().rstrip("/")
    _upstream = up if up.startswith(("http://", "https://")) else ""
    return _peers


reload_config()


def _upstream_hosts() -> set[str]:
    """Hosts the upstream said it shows, minus the ones this board reaches
    itself — its own label and its direct peers win over the detour."""
    with _lock:
        hosts = set((_cache.get(_UPSTREAM) or {}).get("hosts") or ())
    return hosts - set(_peers) - {local_label()}


def _base(label: Optional[str]) -> Optional[str]:
    """The board that runs `label`'s actions: a direct peer's own, the
    upstream for a host only it reaches, None for here."""
    if not label:
        return None
    if label in _peers:
        return _peers[label]
    if _upstream and label in _upstream_hosts():
        return _upstream
    return None


def is_peer(label: str) -> bool:
    """Whether `label` names a host whose board runs its actions — the routes
    that take a host run there when it does, and here otherwise (this host's own
    label or none at all)."""
    return _base(label) is not None


def enabled() -> bool:
    return bool(_peers) or bool(_upstream)


def split_key(key: str) -> tuple[Optional[str], int]:
    """`"b:1234"` → ("b", 1234); `"1234"` → (None, 1234).

    A label that isn't a configured peer resolves to local — including this
    host's own label, so a key minted here round-trips. Raises ValueError if
    the pid half isn't an integer, which routes turn into a 404."""
    label, sep, rest = str(key).partition(":")
    if not sep:
        return None, int(label)
    return (label if is_peer(label) else None), int(rest)


def _request(url: str, method: str, body: Optional[dict], timeout: float,
             view: bool = False) -> tuple[int, Any]:
    """One board-to-board HTTP call. Returns (status, parsed-json).

    An HTTP error carrying a JSON body is returned, not raised: a peer's 404
    for an unknown pid is a real answer and should reach the caller as one."""
    data = None
    headers = {PEER_HEADER: "1", FROM_HEADER: local_label()}
    if view:
        headers[VIEW_HEADER] = "1"
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
    upstream = label == _UPSTREAM
    try:
        status, payload = _request(f"{base}/api/windows", "GET", None, POLL_TIMEOUT,
                                   view=upstream)
    except Exception as e:  # urllib raises URLError, socket.timeout, ssl…
        _record_error(label, f"{type(e).__name__}: {e}")
        return
    if status != 200 or not isinstance(payload, dict):
        _record_error(label, f"HTTP {status}")
        return
    wins = [w for w in payload.get("windows", []) if isinstance(w, dict)]
    gpus = [g for g in payload.get("gpus", []) if isinstance(g, dict)]
    if not upstream:
        # The upstream's view comes addressed already; a peer's local cards and
        # GPU sample (core/gpus.py) are re-addressed here. A board too old to
        # send a GPU sample has no GPU rows.
        wins = [_stamp(w, label) for w in wins]
        gpus = [dict(g, host=label) for g in gpus]
    # How old the peer's snapshot already was when it answered (see STALENESS).
    # A board too old to send it counts as fresh on arrival, as before.
    age = payload.get("age")
    if isinstance(age, bool) or not isinstance(age, (int, float)) or age < 0:
        age = 0.0
    with _lock:
        _cache[label] = {
            "windows": wins,
            "gpus": gpus,
            "ts": time.time(),
            "age": float(age),
            "error": None,
            # Upstream only: the hosts it shows (routing) and how each is doing.
            "hosts": [h for h in payload.get("hosts", []) if isinstance(h, str)],
            "via": payload.get("host") if upstream else None,
            "peers": [p for p in payload.get("peers", []) if isinstance(p, dict)],
        }


def _data_age(entry: dict, now: float) -> float:
    """How old a cache entry's cards are: the time since they arrived plus how
    old they already were then."""
    return now - (entry.get("ts") or 0) + entry.get("age", 0.0)


def _record_error(label: str, msg: str) -> None:
    """Keep the last good windows, only note the failure — a peer that blips
    between polls shouldn't blank its cards. Age is what eventually drops them."""
    with _lock:
        entry = _cache.setdefault(label, {"windows": [], "gpus": [], "ts": 0.0, "age": 0.0})
        entry["error"] = msg


def _poll_loop(label: str, base: str) -> None:
    while True:
        try:
            _poll_once(label, base)
        except Exception as e:
            _record_error(label, f"{type(e).__name__}: {e}")
        time.sleep(POLL_INTERVAL)


def start() -> None:
    """Start one polling thread per configured peer and the upstream. Idempotent."""
    targets = dict(_peers)
    if _upstream:
        targets[_UPSTREAM] = _upstream
    for label, base in targets.items():
        t = _threads.get(label)
        if t and t.is_alive():
            continue
        t = threading.Thread(target=_poll_loop, args=(label, base),
                             name=f"fleet-peer-{label}", daemon=True)
        _threads[label] = t
        t.start()


def _entries() -> dict[str, dict]:
    """A copy of the cache, so readers never hold the lock."""
    with _lock:
        return {k: dict(v) for k, v in _cache.items()}


def _live_entries(direct_only: bool = False) -> list[tuple[dict, bool, Optional[set]]]:
    """(cache entry, stale, hosts to keep) for each configured peer — and,
    unless `direct_only`, the upstream — that answered within DROP_AFTER, from
    the last successful poll. A peer's rows are all kept (None); the upstream's
    only for the hosts that route through it. Never blocks."""
    now = time.time()
    out = []
    for label, entry in _entries().items():
        # a peer removed from the config since it was polled is gone
        if now - (entry.get("ts") or 0) > DROP_AFTER:
            continue
        stale = _data_age(entry, now) > STALE_AFTER
        if label in _peers:
            out.append((entry, stale, None))
        elif label == _UPSTREAM and _upstream and not direct_only:
            out.append((entry, stale, _upstream_hosts()))
    return out


def remote_windows(direct_only: bool = False) -> list[dict]:
    """Every peer's cards, and the upstream's for hosts only it reaches."""
    return [dict(w, peer_stale=stale)
            for entry, stale, keep in _live_entries(direct_only)
            for w in entry.get("windows", []) if keep is None or w.get("host") in keep]


def remote_gpus(direct_only: bool = False) -> list[dict]:
    """Every peer's GPU rows, stale when the peer is or its own sampler is.
    Copies: `gpus.attach` writes card names into them."""
    return [dict(copy.deepcopy(g), stale=stale or bool(g.get("stale")))
            for entry, stale, keep in _live_entries(direct_only)
            for g in entry.get("gpus", []) if keep is None or g.get("host") in keep]


def fleet_view(local: dict) -> dict:
    """What this board answers a downstream board's VIEW_HEADER poll with:
    `local` (its own snapshot) plus its direct peers' cards, each keyed
    "<host>:<pid>" so the key routes back here unchanged. Direct peers only —
    never this board's own upstream — so boards can't feed each other in a loop."""
    label = local.get("host") or local_label()
    mine = [dict(w, key=f"{label}:{w.get('pid')}") for w in local.get("windows", [])]
    return {
        **local,
        "windows": mine + remote_windows(direct_only=True),
        "gpus": list(local.get("gpus", [])) + remote_gpus(direct_only=True),
        "hosts": [label, *sorted(_peers)],
        "peers": status(direct_only=True),
    }


def note_polled_by(sender: str) -> None:
    """Remember that `sender` polled this board just now (see POLLED BY)."""
    if sender:
        with _lock:
            _polled_by[sender[:64]] = time.time()


def polled_by() -> list[str]:
    """Who polled this board in the last POLLED_FOR seconds. Labels only, no
    times: the snapshot carries this, and a ticking value would redraw it."""
    now = time.time()
    with _lock:
        return sorted(k for k, ts in _polled_by.items() if now - ts <= POLLED_FOR)


def status(direct_only: bool = False) -> list[dict]:
    """Per-peer health for the UI header: online, stale, and the last error.
    `stale` is a peer that answers but whose cards are old — one stuck
    mid-refresh (see STALENESS); an offline peer's cards are old too.

    Nothing that moves with the clock (how long ago it answered): the snapshot
    carries this, and a value that ticks would re-send the board every poll."""
    now = time.time()
    entries = _entries()
    out = []
    for label in sorted(_peers):
        entry = entries.get(label) or {}
        ts = entry.get("ts") or 0
        out.append({
            "host": label,
            "online": bool(ts) and now - ts <= STALE_AFTER,
            "stale": bool(ts) and _data_age(entry, now) > STALE_AFTER,
            "error": entry.get("error"),
        })
    if direct_only or not _upstream:
        return out
    # Hosts seen through the upstream: as the upstream sees them, and no better
    # than the upstream itself is doing from here.
    entry = entries.get(_UPSTREAM) or {}
    ts = entry.get("ts") or 0
    up_online = bool(ts) and now - ts <= STALE_AFTER
    up_stale = bool(ts) and _data_age(entry, now) > STALE_AFTER
    seen = {p.get("host"): p for p in entry.get("peers") or ()}
    via = entry.get("via") or "upstream"
    hosts = sorted(_upstream_hosts())
    if not hosts:  # never answered: one row, so the header says so
        return out + [{"host": via, "online": False, "stale": False, "via": via,
                       "error": entry.get("error") or "upstream not polled yet"}]
    for h in hosts:
        p = seen.get(h, {})
        out.append({
            "host": h,
            "online": up_online and p.get("online", True),
            "stale": up_stale or bool(p.get("stale")),
            "error": entry.get("error") if not up_online else p.get("error"),
            "via": via,
        })
    return out


def forward_card(label: str, pid: int, method: str, tail: str,
                 body: Optional[dict] = None) -> Any:
    """Run a per-card route on the board that owns host `label`'s card `pid`.
    A direct peer sees the card as local (`/api/windows/<pid>/…`); the upstream
    routes it on by the full key (`/api/windows/<label>:<pid>/…`)."""
    key = str(pid) if label in _peers else f"{label}:{pid}"
    return _forward(label, method, f"/api/windows/{key}/{tail}", body)


def forward_host(label: str, path: str, body: dict) -> Any:
    """Run a route that names a `host` on the board that runs that host. A
    direct peer gets the body with `host` stripped — it runs the request
    locally, and a peer list that ever names this board back can't bounce it
    around; the upstream keeps it, to know where to send it on."""
    if label in _peers:
        body = {k: v for k, v in body.items() if k != "host"}
    return _forward(label, "POST", path, body)


def _forward(label: str, method: str, path: str, body: Optional[dict] = None) -> Any:
    """Run one request on the board `_base(label)` names and return its answer.

    Errors are surfaced as a normal action result (`{"ok": false, "error": …}`)
    rather than an exception: the buttons on a card all render `error`, so an
    unreachable peer reads as "couldn't reach host b" on the card instead of a
    stack trace in the log and a silent button. `label` is routable: callers
    only forward what split_key or is_peer named one."""
    timeout = POST_TIMEOUT if method == "POST" else GET_TIMEOUT
    base = _base(label)
    if base is None:
        return {"ok": False, "error": f"no route to host {label}"}
    try:
        status_code, payload = _request(f"{base}{path}", method, body, timeout)
    except Exception as e:
        return {"ok": False, "error": f"could not reach host {label}: {type(e).__name__}"}
    if status_code >= 400:
        detail = payload.get("detail") if isinstance(payload, dict) else None
        return {"ok": False, "error": f"host {label}: {detail or f'HTTP {status_code}'}",
                "status": status_code}
    return payload

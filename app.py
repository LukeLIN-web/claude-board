"""Claude Fleet — FastAPI app: dashboard backend + SSE."""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from core import actions, auth, btwcapture, btwlog, codex, history, hmz, memory, patrol, peers, perms, plans, promptqueue, search, sessions, skills, transcripts, tmux, usage

HERE = Path(__file__).parent
STATIC_DIR = HERE / "static"


# ---------- shared in-memory state ----------

# What in a snapshot moves on every tick by itself. Each is left out of the
# watcher's decision to broadcast, or it would re-send the whole board every 2s
# with nothing on it having changed:
#   ts                    when the snapshot was built; the page never shows it.
#   windows[].idle_seconds, windows[].elapsed_s
#                         a card's idle time and a running hmz run's clock, in
#                         seconds as of the snapshot. The page shows each one
#                         advanced by its own clock since the snapshot arrived
#                         (liveSeconds in static/index.html), so a quiet card
#                         goes "3m ago", "4m ago"… without a push.
#   peers[].age_seconds   how long ago a peer last answered. The page shows only
#                         whether it is online, which is a field of its own.
# Everything else counts, whatever it is called. So a value that grows with the
# clock must not be written into a card's text — "空闲 12m" in a reason would
# re-send the board once a minute per quiet card, every second for a fresh one —
# but go out as seconds in one of the fields above, the text holding a marker
# where the page puts it in (patrol.IDLE, hmz.ELAPSED). What the time decides
# stays on the server and does push: a card turning completed, stalled or
# closeable changes its triage, and the header counts with it.
_TICKING_SNAPSHOT = frozenset({"ts"})
_TICKING_CARD = frozenset({"idle_seconds", "elapsed_s"})
_TICKING_PEER = frozenset({"age_seconds"})


class State:
    def __init__(self) -> None:
        self.last_snapshot: dict = {"windows": [], "counts": {}, "ts": 0}
        # The local half of the snapshot above, served verbatim to a peer board
        # asking what *this* host is running (see api_windows). Kept rather than
        # recomputed: an aggregating peer polls every 2s, and enrichment walks
        # transcripts and scrapes panes.
        self.last_local_snapshot: dict = {"windows": [], "counts": {}, "ts": 0}
        self.last_signature: str = ""
        self.subscribers: set[asyncio.Queue] = set()

    def diff_signature(self, snap: dict) -> str:
        """Everything the page can draw from `snap`, as one string the watcher
        compares with the last one it broadcast.

        The whole snapshot rather than a list of the fields that matter: that
        list was kept by hand, and every field nobody remembered to add to it —
        the triage and its reason, the permission message, the current task,
        background tasks, the header counts — reached the page only once
        something that was on it changed too (a busy session going quiet sat on
        "working" until then). Only the ticking values above are left out, so a
        new field pushes when it changes without anyone listing it here."""
        view = {k: v for k, v in snap.items() if k not in _TICKING_SNAPSHOT}
        view["windows"] = [{k: v for k, v in w.items() if k not in _TICKING_CARD}
                           for w in snap["windows"]]
        if "peers" in snap:
            view["peers"] = [{k: v for k, v in p.items() if k not in _TICKING_PEER}
                             for p in snap["peers"]]
        # Sorted keys: the same content always reads the same, whatever order
        # the enrichment happened to set a card's fields in.
        return json.dumps(view, sort_keys=True)


state = State()


# Model labels read off a session's welcome banner, keyed by (pid, session id).
# The transcript is the primary source (it records what actually answered), but
# it only names a model on assistant rows: from a /clear — or a launch — until
# the session's next reply there is nothing in it to read, and the card's model
# readout would go blank on exactly the idle sessions you are deciding what to
# send next. The banner covers that gap, at the price of a capture-pane, which
# this cache keeps off the 2s poll: a banner never changes within one session id,
# and /clear starts a new one, so the next banner is picked up on its own.
_banner_models: dict[tuple[int, str], tuple[str, float]] = {}
# A miss is cached too — a session whose banner has scrolled away must not be
# re-scraped every tick — but only for a while: a pane caught mid-launch, before
# the banner is painted, has to get a second look or the card stays blank until
# the session first replies.
_BANNER_RETRY = 30.0


def _banner_model(w: dict) -> str:
    """The model `w`'s session prints in its welcome banner ("" if unreadable).

    Read from the live pane, so it only answers for a session still running in
    one; a dead or tmux-less window keeps its blank readout rather than borrowing
    a label from somewhere else.
    """
    pid = w.get("pid")
    if not isinstance(pid, int) or not w.get("alive") or not w.get("tty"):
        return ""
    # A `.slock` agent sub-session runs on its parent's tty, so the banner in
    # that pane is the parent's — and a sub-agent is routinely on a different
    # model. Its own transcript answers for it a turn later; until then the card
    # says nothing rather than the window above it.
    if w.get("hidden"):
        return ""
    key = (pid, w.get("session_id") or "")
    hit = _banner_models.get(key)
    if hit and (hit[0] or time.time() - hit[1] < _BANNER_RETRY):
        return hit[0]
    try:
        label = actions.pane_model(w["tty"])
    except Exception:
        label = ""  # scrape failures degrade to no readout, never to a guess
    _banner_models[key] = (label, time.time())
    return label


def _prune_banner_models(live_pids: set) -> None:
    """Drop cached banners for pids that are gone, so the cache tracks the board
    instead of growing an entry per session for the life of the process."""
    for key in list(_banner_models):
        if key[0] not in live_pids:
            _banner_models.pop(key, None)


def _local_snapshot() -> dict:
    """This host's own cards, fully enriched. No peer traffic happens here."""
    snap = sessions.snapshot()
    perm_by_tty = perms.pending_by_tty()
    # Live Codex sessions arrive pre-enriched (codex transcripts have a different
    # shape than Claude's, so they can't go through the loop below). Shell-process
    # counts are platform-agnostic, so we fold their pids into the single ps walk.
    codex_windows = codex.codex_window_dicts()
    hmz_windows = hmz.hmz_window_dicts()
    every = snap["windows"] + codex_windows + hmz_windows
    shell_counts = sessions.shell_descendant_counts(
        [w["pid"] for w in every if isinstance(w.get("pid"), int)]
    )
    for w in every:
        w["shell_proc_count"] = shell_counts.get(w.get("pid"), 0)
    for cw in codex_windows:
        # Codex's status line names the model + effort the session is on right
        # now, a /model pick included; the rollout label it arrives with only
        # says what the last turn ran on.
        live = actions.codex_pane_model(cw.get("tty"))
        if live:
            cw["model_label"] = live
            cw["model_source"] = "pane"
    for w in snap["windows"]:
        tty = w.get("tty")
        if tty and tty in perm_by_tty:
            ev = perm_by_tty[tty]
            w["permission_msg"] = ev.msg
            w["permission_ts"] = ev.raw_ts
        else:
            w["permission_msg"] = None
            w["permission_ts"] = None
        tp = w.get("transcript_path")
        if not w.get("name") and tp:
            from core.history import _extract_first_user_text
            first = _extract_first_user_text(Path(tp))
            if first:
                w["first_input"] = first[:100]
        if tp:
            w["current_task"] = transcripts.current_task_hint(tp)
            # The model that last *answered* — a board-driven switch only shows up
            # here once the session replies on the new model, which is the point:
            # the card can't claim a switch the session never actually made.
            w["model"] = transcripts.current_model(tp)
        else:
            w["current_task"] = None
            w["model"] = ""
        w["model_label"] = transcripts.pretty_model(w["model"])
        w["model_source"] = "transcript" if w["model"] else ""
        if not w["model_label"]:
            banner = _banner_model(w)
            if banner:
                w["model_label"] = banner
                w["model_source"] = "banner"
        # Claude reports waitingFor="dialog open" for ANY open overlay — the
        # /goal panel included, which has nothing to answer and doesn't block
        # the agent. Only a verifiable picker in the pane earns the waiting
        # card (Quick Approve types "1" into the input box otherwise); when
        # the pane shows none, treat the session as busy. An unverifiable
        # pane (no tmux) keeps the conservative waiting card.
        if w.get("status") == "waiting" and w.get("waiting_for") == "dialog open":
            dlg = actions.pane_dialog(w.get("tty"))
            if dlg is not None and not dlg["menu"]:
                w["status"] = "busy"
                w["waiting_for"] = None
            elif dlg is not None and dlg["trust"]:
                # Name the dialog rather than leave the card on "dialog open":
                # the folder-trust prompt is answered by its own route, and it
                # is the one a spawn into a never-opened directory always hits.
                w["waiting_for"] = "trust prompt"
        if tp:
            w["skills_used"] = transcripts.extract_skills_used(tp)
            w["memory_ops"] = transcripts.extract_memory_ops(tp)
            w["background_tasks"] = transcripts.extract_background_tasks(tp)
            # A looping prompt is the one background thing a card can't show any
            # other way: it leaves no shell and no task row, so an unmarked card
            # just looks like a session talking to itself every half hour.
            w["loop"] = transcripts.session_loop(tp)
        else:
            w["skills_used"] = []
            w["memory_ops"] = []
            w["background_tasks"] = []
            w["loop"] = None
        # After background_tasks, not before: that list is how patrol tells a
        # session waiting on background work from one waiting on a person.
        tri = patrol.classify(w)
        w["triage"] = tri["triage"]
        w["triage_reason"] = tri["reason"]
        w["triage_suggestion"] = tri["suggestion"]
        # Queued prompts: reliable dashboard-sent items (reconciled against the
        # transcript) plus best-effort TUI-typed items scraped from the pane.
        # A queue only exists while busy, which also bounds the extra capture.
        pid = w.get("pid")
        status = w.get("status")
        # A queue only exists while the session is working. Real windows report
        # "busy"; hidden `.slock` agent sub-sessions never write a status field
        # (it normalizes to "unknown"), so gate those on being alive instead —
        # their pid+tty still back both the tracker and the pane scrape.
        show_queue = isinstance(pid, int) and (
            status == "busy" or (w.get("hidden") and w.get("alive"))
        )
        if status == "idle" and isinstance(pid, int):
            promptqueue.clear(pid)  # a queue can't outlive an idle session
        if show_queue:
            dash = promptqueue.pending(pid, tp)
            queued = [{"text": t, "source": "dashboard"} for t in dash]
            seen = {promptqueue.norm(t) for t in dash}
            try:
                for t in actions.get_pane_queue(w.get("tty")):
                    nt = promptqueue.norm(t)
                    if nt and nt not in seen:
                        seen.add(nt)
                        queued.append({"text": t, "source": "tui"})
            except Exception:
                pass  # scrape failures degrade to dashboard-only
            w["queued"] = queued
        else:
            w["queued"] = []
        # /btw asides never reach the transcript, so scrape the ephemeral overlay
        # from the pane (best-effort, only while it is on-screen) and latch it to
        # disk. The overlay can be open whether the session is busy or idle, so
        # this isn't gated on show_queue. w["btw"] shows the latest archived aside
        # and persists after the overlay is dismissed.
        sid = w.get("session_id")
        pending_q = None
        if isinstance(pid, int) and w.get("tty") and sid:
            # A long answer only shows a slice in the overlay window; recovering the
            # rest means scrolling the pane, so this gates on a cheap top-slice
            # scrape and does the slow scroll-stitch off-thread (core.btwcapture).
            pending_q = btwcapture.maybe_capture(w["tty"], sid)
        w["btw"] = btwlog.latest(sid) if sid else None
        if pending_q is not None:
            # An aside whose answer is still generating: show it live so /btw
            # never looks dead while it works (nothing is archived yet).
            w["btw"] = {"question": pending_q, "answer": "", "pending": True}
    _prune_banner_models({w.get("pid") for w in snap["windows"]})
    # Merge live Codex and hmz windows in, then address every card. `key` — not pid — is
    # what the UI and the action routes carry: once a peer host's cards sit in
    # the same list, pids alone collide (see core/peers.py).
    snap["windows"].extend(codex_windows + hmz_windows)
    label = peers.local_label()
    for w in snap["windows"]:
        w["host"] = label
        w["key"] = str(w.get("pid"))
    snap["host"] = label
    # Capability flag for the UI to gate the tmux-backed controls. `available()`
    # is cached, so this does not spawn a tmux subprocess on every 2s poll.
    snap["tmux_available"] = tmux.available()
    _finalize_snapshot(snap)
    return snap


def _finalize_snapshot(snap: dict) -> None:
    """Recompute the header counts over every visible (non-hidden) window and
    put the cards in display order. Counts go by `triage`, not the raw `status`:
    the header chips filter cards on triage, so a session stuck at status=busy
    but idle past the threshold (triage=completed) must land in the idle tally —
    otherwise it inflates "busy" yet vanishes when you click the busy filter.

    Run over the merged list too, so a peer's cards are counted and ordered
    beside the local ones instead of being appended after them."""
    visible = [w for w in snap["windows"] if not w.get("hidden")]
    busy = [w for w in visible if w.get("triage") == "working"]
    waiting = [w for w in visible if w.get("triage") == "waiting_perm"]
    snap["counts"] = {
        "total": len(visible),
        "busy": len(busy),
        "waiting": len(waiting),
        "idle": len(visible) - len(busy) - len(waiting),
    }
    # Sort by triage priority (most urgent first), then by idle time.
    snap["windows"].sort(key=lambda w: (
        patrol.TRIAGE_PRIORITY.get(w.get("triage", ""), 99),
        -w.get("updated_at", 0),
    ))


def _enriched_snapshot() -> dict:
    """The board's view: this host's cards plus every peer's, in one list.

    Peer cards come from core.peers' cache, which a background thread refills —
    nothing here waits on the network, so a wedged peer costs its own cards
    going stale and nothing else."""
    local = _local_snapshot()
    state.last_local_snapshot = local
    if not peers.enabled():
        return local
    # Shallow copy with a fresh window list: the local snapshot is handed to
    # peers verbatim and must not grow their cards back into it.
    merged = dict(local)
    remote = peers.remote_windows()
    merged["windows"] = list(local["windows"]) + remote
    merged["peers"] = peers.status()
    # The UI gates the tmux controls on one flag. A card whose own host has no
    # tmux still gets buttons; pressing one returns that host's error, which is
    # a clearer answer than a control that silently isn't there.
    merged["tmux_available"] = bool(local.get("tmux_available")) or any(
        p.get("online") for p in merged["peers"]
    )
    _finalize_snapshot(merged)
    return merged


async def _watcher() -> None:
    """Poll sessions every 2s; broadcast deltas to SSE subscribers."""
    while True:
        try:
            # Off the event loop: building a snapshot parses transcripts and
            # runs tmux/ps subprocesses, and doing that on the loop stalls every
            # SSE stream and request (the login page included) for as long as
            # it takes. Comparing and broadcasting stay here, on the loop that
            # owns the subscriber queues.
            snap = await asyncio.to_thread(_enriched_snapshot)
            sig = state.diff_signature(snap)
            state.last_snapshot = snap
            if sig != state.last_signature:
                state.last_signature = sig
                payload = json.dumps(snap)
                dead: list[asyncio.Queue] = []
                for q in list(state.subscribers):
                    try:
                        q.put_nowait(payload)
                    except asyncio.QueueFull:
                        dead.append(q)
                for q in dead:
                    state.subscribers.discard(q)
        except Exception as e:
            print(f"[watcher] error: {e}")
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    peers.start()
    task = asyncio.create_task(_watcher())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="Claude Fleet", lifespan=lifespan)

# The password gate, if .env.local sets FLEET_AUTH_PASSWORD. This registers the
# login routes and wraps the whole ASGI app, so every route below — and every
# route added later — is behind it by construction. See core/auth.py for why
# there is no loopback exemption. With no password set it is a pass-through and
# the board behaves exactly as it always has on 127.0.0.1; scripts/cf-tunnel.sh
# is the piece that refuses to publish an ungated board.
auth.install(app)


# ---------- routes ----------

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text()
    html = _apply_instance_label(html)
    # The UI is a single hand-edited HTML file with no asset versioning, so tell
    # the browser to always revalidate — otherwise a stale cached copy hides new
    # features (e.g. the permission / question controls) until a hard refresh.
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, must-revalidate"})


def _apply_instance_label(html: str) -> str:
    """Stamp a per-host label into the tab title and header so multiple
    dashboards are tellable apart. Defaults to the host IP's last octet
    (e.g. "60"); CLAUDE_FLEET_LABEL overrides it. No label resolved ⇒ HTML is
    returned unchanged. Same label the cards carry as `host` (core/peers.py), so
    the header badge and a local card's badge always agree."""
    label = peers.local_label()
    if not label:
        return html
    html = html.replace(
        "<title>Claude Fleet</title>",
        f"<title>Claude Fleet · {label}</title>",
    )
    html = html.replace(
        '<h1 class="text-xl font-bold tracking-tight">Claude Fleet</h1>',
        '<h1 class="text-xl font-bold tracking-tight">Claude Fleet'
        ' <span class="text-xs font-normal align-middle bg-slate-200'
        ' text-slate-700 px-2 py-0.5 rounded-full">' + label + "</span></h1>",
    )
    return html


@app.get("/api/windows")
def api_windows(request: Request) -> dict:
    # A peer board asking what *this* host is running gets the local half only.
    # Without this, two boards configured as each other's peer would each serve
    # the other its own cards back and the page would show every session twice.
    # Built here only until the watcher has built one (ts 0) — not whenever the
    # list is empty, which on a host with no sessions is every poll.
    if request.headers.get(peers.PEER_HEADER):
        if not state.last_local_snapshot["ts"]:
            state.last_local_snapshot = _local_snapshot()
        return state.last_local_snapshot
    if not state.last_snapshot["ts"]:
        state.last_snapshot = _enriched_snapshot()
    return state.last_snapshot


def _split(key: str) -> tuple[str | None, int]:
    """Card key → (peer host or None, pid on that host). A malformed key is a
    404, the same answer an unknown pid gets."""
    try:
        return peers.split_key(key)
    except ValueError:
        raise HTTPException(404, "window not found")


def _require_window(pid: int):
    """Resolve a pid to a *visible* window or 404. `find_window` already honors
    the CLAUDE_FLEET_CWD_INCLUDE/EXCLUDE filter, so this also blocks actions
    against hidden sessions, not just unknown pids."""
    w = sessions.find_window(pid)
    if not w:
        raise HTTPException(404, "window not found")
    return w


def _on_card(key: str, method: str, tail: str, local: Callable, body: BaseModel | None = None):
    """Run a per-card route where the card lives. A peer's card is forwarded to
    that board as `/api/windows/<pid>/<tail>` with the request body untouched;
    a local one is resolved with _require_window and handed to `local(w, pid)`."""
    host, pid = _split(key)
    if host:
        return peers.forward(host, method, f"/api/windows/{pid}/{tail}",
                             body.model_dump() if body is not None else None)
    return local(_require_window(pid), pid)


def _claude_panels(tp: str) -> dict:
    """What a Claude transcript pins beside its timeline — on a live card's
    panel and an archived session's alike."""
    if not tp:
        return {"skills_used": [], "memory_ops": [], "plan_history": [],
                "goal": None, "loop": None}
    return {
        "skills_used": transcripts.extract_skills_used(tp),
        "memory_ops": transcripts.extract_memory_ops(tp),
        "plan_history": transcripts.extract_plan_history(tp),
        # The session's standing goal, pinned above the timeline so it stays put
        # while the events that stated it scroll away.
        "goal": transcripts.session_goal(tp),
        # The /loop prompt this session keeps re-running, pinned for the same
        # reason — and because it explains turns arriving that nobody just sent.
        "loop": transcripts.session_loop(tp),
    }


@app.get("/api/windows/{key}/timeline")
def api_timeline(key: str, limit: int = 2000) -> dict:
    return _on_card(key, "GET", f"timeline?limit={limit}",
                    lambda w, pid: _timeline(w, pid, limit))


def _timeline(w, pid: int, limit: int) -> dict:
    """A local card's timeline panel: its events and what is pinned above them."""
    tp = w.transcript_path or ""
    out = {
        "pid": pid,
        "session_id": w.session_id,
        "project_name": w.project_name,
        # Defaults, which Codex and hmz mostly keep: neither writes a recap, so
        # there is no goal to pin, nor has a /loop skill, so nothing schedules
        # a looping prompt — and neither has a Claude-style interactive menu to
        # scrape from the pane.
        "skills_used": [],
        "memory_ops": [],
        "plan_history": [],
        "goal": None,
        "loop": None,
        "menu": None,
    }
    if w.platform == "codex":
        # Codex transcripts have their own shape.
        activity = codex.extract_codex_session_activity(tp) if tp else {}
        out.update(
            platform="codex",
            events=codex.codex_timeline(tp, limit=limit, since_ms=codex.cleared_at_ms(pid)) if tp else [],
            skills_used=activity.get("skills_used", []),
            memory_ops=activity.get("memory_ops", []),
        )
        return out
    if w.platform == "hmz":
        # What was typed into it too: hmz takes a line it then refuses, and that
        # line is in no run.
        typed = hmz.typed(pid, w.cwd, w.started_at)
        cleared = hmz.cleared_at_ms(typed, codex.cleared_at_ms(pid))
        crumb = hmz.menu(w)
        events = hmz.hmz_timeline(tp or None, limit=limit, typed=typed, since_ms=cleared)
        out.update(
            platform="hmz",
            events=events,
            note=(hmz.MENU_NOTE.format(crumb) if crumb
                  else hmz.CLEARED_NOTE if cleared and not events else None if tp
                  else hmz.TYPED_NO_RUN_NOTE if typed else hmz.NO_RUN_NOTE),
        )
        return out
    events = transcripts.timeline(tp, limit=limit) if tp else []
    # Merge in /btw asides — they live only in the fleet's archive (never the
    # transcript). Re-sort by timestamp so they interleave with real turns;
    # only sort when there is something to merge, to avoid perturbing the
    # transcript's own ordering otherwise.
    btw_evs = btwlog.timeline_events(w.session_id) if w.session_id else []
    if btw_evs:
        events = sorted(events + btw_evs,
                        key=lambda e: transcripts._parse_ts(e.get("ts", "")))[-limit:]
    out.update(
        platform="claude",
        events=events,
        **_claude_panels(tp),
        # Live interactive menu (AskUserQuestion / permission prompt) parsed from
        # the tmux pane — the transcript doesn't record it until it's resolved.
        menu=actions.get_pane_menu(pid),
    )
    return out


@app.get("/api/windows/{key}/plan")
def api_plan(key: str) -> dict:
    return _on_card(key, "GET", "plan", lambda w, pid: {
        "pid": pid, "plan": plans.plan_for_session(w.name, w.transcript_path)})


@app.get("/api/search")
def api_search(q: str, limit: int = 60) -> dict:
    if not q.strip():
        return {"hits": [], "q": q}
    return {"hits": search.search(q, limit=limit), "q": q}


@app.get("/api/plans")
def api_plans() -> dict:
    return {"plans": plans.list_plans()}


@app.post("/api/windows/{key}/focus")
def api_focus(key: str) -> dict:
    return _on_card(key, "POST", "focus", lambda w, pid: (
        actions.focus_terminal(w.tty) if w.tty
        else {"ok": False, "error": "no tty available for this pid"}))


class CreateBody(BaseModel):
    cwd: str
    platform: str = "claude"  # "claude" | "codex" | "hmz"
    # Which board runs the spawn. Empty (or this host's own label) means here;
    # a configured peer label spawns on that machine, in that machine's paths.
    host: str = ""


class PromptBody(BaseModel):
    text: str


@app.post("/api/windows/create")
def api_window_create(body: CreateBody) -> dict:
    if body.host and body.host in peers.configured():
        # Forward with the host stripped: the peer spawns locally, and a peer
        # list that ever names this board back can't bounce the request around.
        return peers.forward(body.host, "POST", "/api/windows/create",
                             {"cwd": body.cwd, "platform": body.platform})
    if not sessions._cwd_visible(body.cwd):
        raise HTTPException(403, "cwd is hidden by the dashboard filter")
    return actions.create_session(body.cwd, body.platform)


class SpawnDirsBody(BaseModel):
    paths: list[str]
    host: str = ""


@app.post("/api/spawn-dirs")
def api_spawn_dirs(body: SpawnDirsBody) -> dict:
    """The subset of `paths` a spawn on `host` would accept: directories that
    exist there and pass that board's cwd filter — the same two checks
    api_window_create makes. Lets the New session picker offer the directories
    other machines' cards are in, without offering one that only exists on them
    (a host-local /home path, say)."""
    if body.host and body.host in peers.configured():
        return peers.forward(body.host, "POST", "/api/spawn-dirs", {"paths": body.paths})
    ok = [p for p in body.paths[:200]
          if sessions._cwd_visible(p) and os.path.isdir(os.path.expanduser(p))]
    return {"ok": True, "paths": ok}


class UsageBody(BaseModel):
    host: str = ""


@app.post("/api/usage")
def api_usage(body: UsageBody) -> dict:
    """Claude plan usage on `host`: how much of the session and weekly limits is
    gone and when each resets (core/usage.py). Each machine has its own login,
    so a peer's is read by the peer's board."""
    if body.host and body.host in peers.configured():
        return peers.forward(body.host, "POST", "/api/usage", {})
    return usage.read_usage()


def _send_recorded(pid: int, text: str) -> dict:
    """Type `text` into the session and, if that worked, record it as sent from
    the dashboard — what the card's Queued list is built from (promptqueue)."""
    # Stamp the send time before the keystrokes: send_prompt types, verifies the
    # composer and verifies the submit, which takes long enough that Claude's own
    # transcript row for the prompt can predate a post-send stamp (see record_sent).
    sent_at = time.time()
    r = actions.send_prompt(pid, text)
    if r.get("ok"):
        promptqueue.record_sent(pid, text, ts=sent_at)
    return r


@app.post("/api/windows/{key}/prompt")
def api_window_prompt(key: str, body: PromptBody) -> dict:
    return _on_card(key, "POST", "prompt",
                    lambda w, pid: _send_recorded(pid, body.text), body)


@app.post("/api/windows/{key}/clear")
def api_window_clear(key: str) -> dict:
    """Send /clear and blank the card's pre-clear preview.

    Claude, Codex and hmz all have /clear. Claude starts a fresh transcript so
    its card empties on its own, but Codex's /clear leaves the rollout JSONL
    intact, and hmz's clears only its screen — so we also stamp a per-pid clear
    time that hides older events from the card and timeline (see
    codex.mark_cleared, hmz.cleared_at_ms)."""
    def clear(w, pid: int) -> dict:
        r = _send_recorded(pid, "/clear")
        if r.get("ok"):
            codex.mark_cleared(pid)
        return r
    return _on_card(key, "POST", "clear", clear)


class ModelBody(BaseModel):
    model: str = ""   # Claude: a /model dialog alias ("opus" | "fable"); Codex: a model name
    effort: str = ""  # Codex only: low | medium | high | xhigh


@app.post("/api/windows/{key}/model")
def api_window_model(key: str, body: ModelBody) -> dict:
    """Switch this session's model (Claude: for the running session only).

    Not a plain `/model <alias>` prompt: that form also saves the pick as the
    user's default for new sessions. actions.switch_model drives the dialog and
    commits with "s" instead (see its docstring). A Codex card goes through its
    two-step picker with `model` and/or `effort`; that picker has no session-only
    scope and also rewrites ~/.codex/config.toml."""
    return _on_card(key, "POST", "model",
                    lambda w, pid: actions.switch_model(pid, body.model, body.effort), body)


class PermissionBody(BaseModel):
    choice: str  # approve | approve_always | deny


@app.post("/api/windows/{key}/permission")
def api_window_permission(key: str, body: PermissionBody) -> dict:
    return _on_card(key, "POST", "permission",
                    lambda w, pid: actions.respond_permission(pid, body.choice), body)


class MenuKeysBody(BaseModel):
    keys: list[str]  # e.g. ["2"], ["Enter"], ["Escape"]


@app.post("/api/windows/{key}/keys")
def api_window_keys(key: str, body: MenuKeysBody) -> dict:
    return _on_card(key, "POST", "keys",
                    lambda w, pid: actions.send_menu_keys(pid, body.keys), body)


@app.post("/api/windows/{key}/close")
def api_close(key: str) -> dict:
    return _on_card(key, "POST", "close", lambda w, pid: actions.close_session(pid))


class BtwDismissBody(BaseModel):
    id: int  # btwlog entry id (w["btw"].id on the card)


@app.post("/api/windows/{key}/btw/dismiss")
def api_btw_dismiss(key: str, body: BtwDismissBody) -> dict:
    """Hide the card's archived /btw aside. Card-state only: the aside stays in
    the archive and the timeline (that's history), it just stops occupying the
    card."""
    def dismiss(w, pid: int) -> dict:
        sid = getattr(w, "session_id", None)
        if not sid:
            return {"ok": False, "error": "window has no session id"}
        if not btwlog.dismiss(sid, body.id):
            return {"ok": False, "error": "aside not found"}
        return {"ok": True}
    return _on_card(key, "POST", "btw/dismiss", dismiss, body)


@app.get("/api/locate/{session_id}")
def api_locate(session_id: str) -> dict:
    """Reverse lookup: session id (or unique >=8-char prefix) → tmux pane.

    External tools that hold a session id — overseer skills, scripts, humans
    reading a transcript filename — use this to find where the session lives
    instead of reverse-engineering pane contents."""
    w = sessions.find_window_by_session(session_id)
    if not w:
        raise HTTPException(404, "no session matches that id")
    pane = tmux.pane_for_tty(w.tty) if w.tty else None
    return {
        "window": w.to_dict(),
        "tmux_pane": pane,
        "tmux_target": tmux.pane_target(pane) if pane else None,
    }


@app.get("/api/history")
def api_history(q: str = "", page: int = 1, limit: int = 30) -> dict:
    return history.list_sessions(q=q or None, page=page, limit=limit)


@app.get("/api/history/{session_id}/timeline")
def api_history_timeline(session_id: str, limit: int = 2000) -> dict:
    # Claude Code transcripts — none at all on a host that runs only Codex or
    # OpenCode, where there is no projects/ dir to list.
    from core.sessions import PROJECTS_DIR
    for proj_dir in (PROJECTS_DIR.iterdir() if PROJECTS_DIR.is_dir() else ()):
        if not proj_dir.is_dir():
            continue
        f = proj_dir / f"{session_id}.jsonl"
        if f.exists():
            if not sessions.transcript_visible(f):
                raise HTTPException(404, "session not found")
            fp = str(f)
            events = transcripts.timeline(fp, limit=limit)
            # The pinned panels too: what the session was for is still worth
            # reading in the archive.
            return {
                "session_id": session_id, "project_slug": proj_dir.name,
                "events": events, "platform": "claude",
                **_claude_panels(fp),
            }
    # Codex rollouts, judged like Claude's on the cwd they record.
    f = codex.find_rollout(session_id)
    if f:
        if not sessions.transcript_visible(f):
            raise HTTPException(404, "session not found")
        events = codex.codex_timeline(str(f), limit=limit)
        return {"session_id": session_id, "project_slug": "codex", "events": events, "platform": "codex"}
    # OpenCode sessions (SQLite), judged on the directory the session records.
    try:
        from core import opencode
        directory = opencode.session_directory(session_id)
        events = (opencode.opencode_timeline(session_id, limit=limit)
                  if directory is not None and sessions._cwd_visible(directory) else [])
    except Exception:
        events = []
    if events:
        return {"session_id": session_id, "project_slug": "opencode", "events": events, "platform": "opencode"}
    raise HTTPException(404, "transcript not found")


def _history_cwd(session_id: str) -> str | None:
    """The directory a history session ran in (home if the index has none), or
    None when the history index doesn't know the session."""
    s = history.get(session_id)
    return (s.project or str(Path.home())) if s else None


@app.post("/api/history/{session_id}/resume")
def api_history_resume(session_id: str) -> dict:
    # If the session is alive, focus it instead of opening a new window.
    w = sessions.find_window_by_session(session_id)
    if w and w.alive and w.tty:
        result = actions.focus_terminal(w.tty)
        return {"ok": result.get("ok", False), "action": "focused", "session_id": session_id, "pid": w.pid}

    cwd = _history_cwd(session_id)
    if cwd is None:
        return {"ok": False, "error": "session not found in index"}
    # Answers the "resume from summary?" picker a large/old session parks on
    # (see actions.resume_claude), so the card isn't left stuck on a menu.
    r = actions.resume_claude(cwd, session_id)
    r.update({"action": "resumed", "session_id": session_id, "cwd": cwd})
    return r


@app.post("/api/history/{session_id}/fork")
def api_history_fork(session_id: str) -> dict:
    cwd = _history_cwd(session_id)
    if cwd is None:
        return {"ok": False, "error": "session not found in index"}
    r = actions.resume_claude(cwd, session_id, fork=True)
    r.update({"action": "forked", "session_id": session_id, "cwd": cwd})
    return r


# What each session's history-index breakdown counts, keyed by its field there
# and named as the reverse-lookup rows below name it. The index produced these
# per session already, for Claude, OpenCode and Codex alike.
_SKILL_KINDS = {"per_skill_invokes": "invoke", "per_skill_reads": "reads",
                "per_skill_writes": "writes", "per_skill_bash_refs": "bash_refs"}
_MEMORY_KINDS = {"per_memory_reads": "reads", "per_memory_writes": "writes",
                 "per_memory_edits": "edits"}


def _sessions_touching(name: str, breakdown_key: str, kinds: dict[str, str]) -> dict:
    """Reverse lookup: the sessions whose `breakdown_key` counts `name` under
    any of `kinds`, busiest first, with the per-kind counts."""
    rows = []
    for s in history.index():
        bd = getattr(s, breakdown_key) or {}
        counts = {field: (bd.get(k) or {}).get(name, 0) for k, field in kinds.items()}
        total = sum(counts.values())
        if total == 0:
            continue
        rows.append({
            "session_id": s.session_id,
            "project_name": s.project_name,
            "platform": s.platform,
            "title": s.first_input[:120],
            "ts": s.last_ts or s.first_ts or "",
            **counts,
            "total": total,
        })
    rows.sort(key=lambda r: -r["total"])
    return {"name": name, "sessions": rows, "session_count": len(rows)}


@app.get("/api/skills/{name}/sessions")
def api_skill_sessions(name: str) -> dict:
    """Reverse lookup: which sessions touched this skill, with per-session counts."""
    return _sessions_touching(name, "skill_breakdown", _SKILL_KINDS)


@app.get("/api/memory/{name}/sessions")
def api_memory_sessions(name: str) -> dict:
    """Reverse lookup: which sessions read/wrote this memory."""
    return _sessions_touching(name, "memory_breakdown", _MEMORY_KINDS)


@app.get("/api/memory/{name}")
def api_memory_detail(name: str) -> dict:
    f = memory.find_memory(name)
    if not f:
        raise HTTPException(404, "memory not found")
    fm, body = memory.split_frontmatter(f.read_text(errors="replace"))
    return {
        "name": fm.get("name", name),
        "description": fm.get("description", ""),
        "type": fm.get("type", "unknown"),
        "content": body,
        "path": str(f),
    }


@app.get("/api/skills")
def api_skills() -> dict:
    session_count: Counter[str] = Counter()
    activity = {k: Counter() for k in _SKILL_KINDS}
    for s in history.index():
        session_count.update(s.skills_used)
        # Use the per-session breakdown that history index already produced
        # (covers Claude + OpenCode + Codex uniformly).
        bd = s.skill_breakdown or {}
        for k, c in activity.items():
            c.update(bd.get(k) or {})
    all_skills = skills.list_all_skills()
    for s in all_skills:
        name = s["name"]
        n = {field: activity[k][name] for k, field in _SKILL_KINDS.items()}
        s["session_count"] = session_count[name]
        s["invoke_count"] = n["invoke"]
        s["reads"] = n["reads"]
        s["writes"] = n["writes"]
        s["bash_refs"] = n["bash_refs"]
        s["total_activity"] = sum(n.values())
    all_skills.sort(key=lambda s: (-s["total_activity"], -s["invoke_count"], s["name"]))
    return {"skills": all_skills}


@app.get("/api/memory")
def api_memory(project: str | None = None) -> dict:
    read_count: dict[str, int] = {}
    write_count: dict[str, int] = {}
    for s in history.index():
        for m in s.memory_ops:
            name = m["name"]
            if m["operation"] == "read":
                read_count[name] = read_count.get(name, 0) + 1
            else:
                write_count[name] = write_count.get(name, 0) + 1
    result = memory.list_memories(project_slug=project)
    for group_mems in result.get("groups", {}).values():
        for m in group_mems:
            stem = m.get("file_stem", m["name"])
            m["read_sessions"] = read_count.get(stem, 0)
            m["write_sessions"] = write_count.get(stem, 0)
    return result


@app.get("/api/perms")
def api_perms() -> dict:
    return perms.snapshot()


@app.get("/api/events")
async def api_events(request: Request) -> EventSourceResponse:
    queue: asyncio.Queue = asyncio.Queue(maxsize=32)
    state.subscribers.add(queue)

    async def event_gen():
        # Send the current snapshot once immediately.
        yield {"event": "snapshot", "data": json.dumps(state.last_snapshot)}
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=20.0)
                    yield {"event": "snapshot", "data": payload}
                except asyncio.TimeoutError:
                    yield {"event": "heartbeat", "data": str(int(time.time()))}
        finally:
            state.subscribers.discard(queue)

    return EventSourceResponse(event_gen())

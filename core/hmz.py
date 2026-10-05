"""Live humanize (`hmz`) TUIs as dashboard cards.

`hmz` with no command opens its terminal interface, which runs flows that drive
the claude / codex CLIs. Its composer is a ❯ prompt between two rules, the same
shape as Claude Code's, so the board types into it on Claude's send path.

What the card reports comes from the run, not the TUI. Every run of a flow is an
epic, ~/.hmz/epics/<workspace>/<when>-<which>/epic.jsonl, one event a line:
`began` (flow, task, the agent each role runs, the `budget`), `opened` (a
session an agent opened), `called` / `returned` (a flow it called), `usage`
(what the run came to, written as it ends) and `ended` (`how`: done, failed or
stopped). <workspace> is the cwd with every non-alphanumeric character turned
into "-", and the TUI reopens on the newest run of its directory — so the card
reads that one: begun and not ended means a flow is running.

What a run spends is on the card too. hmz's status bar bills a run as it goes
off the logs its agents write — each turn's tokens by kind, priced per model
from its copy of openllmprices.com, <home>/prices.json — and the card reads the
same logs the same way (see _spending). A run that is over carries hmz's own
figures instead, on its `usage` line: `cost` in USD, the `output_tokens` its
agents wrote, the `seconds` they spent in turns. The `budget` caps what a run
may spend (`duration`, `cost`, `output_tokens`; the chat flow runs under
`cost: Infinity`, no cap), and its `graceful` says what happens to the turn
under way when a cap is hit: let finish and the next refused (the default), or
cut off at once — "even mid-turn", as hmz's own budget menu words it.

What the agents said is not in the epic but beside it. A flow another flow
called writes its own record, epic.<flow>_<id>.jsonl in the same directory, and
the sessions opened inside it are written down there. Each session an agent
opens is kept in the run too, under <epic>/sessions/<cli>/ laid out as that CLI
lays out its home — projects/<dir>/<id>.jsonl for Claude, sessions/<y>/<m>/<d>/
rollout-…-<id>.jsonl for Codex — and its `opened` line says where (`where`,
relative to the epic, or whole for a session that stayed in the CLI's own
home). Not in ~/.claude or ~/.codex, so no card of its own: the hmz card is the
only place those sessions show.

~/.hmz is $HUMANIZE_HOME when the hmz was started with one, as hmz's own
`home()` has it. It was ~/.humanize until hmz renamed it: a newer hmz moves the
old one over the first time it runs, and an older one goes on using it.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from pathlib import Path
from typing import Callable, Optional

from . import codex, tmux, transcripts
from .codex import _classify_codex, _proc_start_ms, _proc_table
from .sessions import HOME_BASE, Window, _cwd_to_project_slug, _cwd_visible, _pid_alive, get_tty
from .textcap import MESSAGE_CHARS, cap_text

HMZ_HOME = HOME_BASE / ".hmz"
HMZ_HOME_WAS = HOME_BASE / ".humanize"

# What the timeline says for an hmz that hasn't run anything yet: there is no
# epic to read until the first line is submitted in it.
NO_RUN_NOTE = ("这个 hmz 还没开始 run，没有可显示的内容。在它的输入框里提交一行后才会有："
               "普通的一行交给当前 flow（状态栏上 ◉ 后面那个），`$<flow> <任务>` 启动指定的 flow。")
# …and for one that was typed into but started nothing: hmz wrote the lines down,
# then answered each on its own screen only.
TYPED_NO_RUN_NOTE = ("这个 hmz 还没开始 run：下面是输入给它的行，它收下了，但没有一行启动 flow。"
                     "它为什么不跑只写在它自己的屏幕上（比如 `hmz: no such flow: …`）。")

# What the timeline says while hmz sits in a menu, which only its terminal can
# answer. A `$flow` its directory hasn't set up lands in one, holding the line.
MENU_NOTE = ("hmz 停在菜单「{}」上，要在它的终端里操作。第一次用某个 flow 时会先弹它的配置菜单"
             "（各角色用什么 agent、参数、预算），填完 Save 才会用你输入的那一行开跑。")

# The breadcrumb hmz tops each menu with — `hmz › parallel_flame_chase › Set
# budget for parallel_flame_chase` — and `● unsaved` beside it mid-edit.
_CRUMB = re.compile(r"^\s*hmz › (.+?)(?:\s{2,}●.*)?\s*$")

# Commands that are not the interface: `hmz exec` runs a flow headless, and
# `hmz internal …` is the sandbox / credential plumbing under every turn.
_HEADLESS = {"exec", "internal"}
_PLAIN = re.compile(r"[^A-Za-z0-9]")

# Where a CLI logs one session under the directory hmz keeps it in, as hmz's own
# backend profiles have it (hmz.coganchor.backends, `logs=`). Claude's subagent
# logs are left out: the card follows the agents the flow drove.
_LOGS = {
    "claude": "projects/*/{}.jsonl",
    "codex": "sessions/**/rollout-*{}.jsonl",
}
# …except on the bill. A sub-agent Claude starts writes a transcript of its own
# under its session, and hmz counts what it spends as the run's.
_SUBAGENT_LOGS = {"claude": "projects/*/{}/subagents/*.jsonl"}


def _is_interactive_hmz(args: str) -> bool:
    """True for an `hmz` process that is its terminal interface."""
    toks = args.split()
    for i, t in enumerate(toks[:3]):
        if os.path.basename(t) == "hmz":
            return not any(a in _HEADLESS for a in toks[i + 1:])
    return False


def _home(pid: int) -> Path:
    """Where hmz `pid` keeps its runs and its history."""
    try:
        env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        env = []
    for kv in env:
        if kv.startswith(b"HUMANIZE_HOME="):
            v = kv.split(b"=", 1)[1].decode(errors="replace")
            if v:
                return Path(v)
    return _default_home()


def _default_home() -> Path:
    """~/.hmz once there is one — hmz uses it, and leaves the old one alone,
    from then on — else ~/.humanize, which an hmz from before the rename keeps."""
    return HMZ_HOME if HMZ_HOME.exists() or not HMZ_HOME_WAS.exists() else HMZ_HOME_WAS


def _latest_epic(cwd: str, home: Optional[Path] = None) -> Optional[Path]:
    """epic.jsonl of the newest run in `cwd`, or None before the first one."""
    runs = (home or _default_home()) / "epics" / _PLAIN.sub("-", cwd)
    try:
        names = sorted(n for n in os.listdir(runs) if (runs / n / "epic.jsonl").is_file())
    except OSError:
        return None
    return runs / names[-1] / "epic.jsonl" if names else None


def _events(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        with path.open() as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if isinstance(d, dict):
                    out.append(d)
    except OSError:
        pass
    return out


def _began(events: list[dict]) -> dict:
    return next((e for e in events if e.get("event") == "began"), {})


def _ended(events: list[dict]) -> dict:
    return next((e for e in reversed(events) if e.get("event") == "ended"), {})


def _records(epic: Path) -> list[Path]:
    """The run's own record, then one per flow it called: the sessions opened
    inside a called flow are written down in that flow's record."""
    try:
        called = sorted(p for p in epic.parent.glob("epic.*.jsonl") if p.is_file())
    except OSError:
        called = []
    return [epic] + called


def _opened(epic: Path) -> list[dict]:
    """Every `opened` line of the run, across all its records, oldest first."""
    lines = [e for r in _records(epic) for e in _events(r)
             if e.get("event") == "opened" and e.get("session")]
    return sorted(lines, key=lambda e: str(e.get("at", "")))


def _logs(epic: Path, opened: dict, of: dict = _LOGS) -> list[Path]:
    """The log files of the session an `opened` line names — its own, or with
    `of` those of what it started — or [] for a CLI the board can't read or a
    log that has gone."""
    ident = str(opened["session"])
    pattern = of.get(str(opened.get("backend") or ""))
    if not pattern:
        return []
    where = str(opened.get("where") or "")
    if where:
        # Relative to the epic for a session kept in the run, whole for one that
        # stayed in its CLI's home — and `/` keeps a whole path whole.
        at, pattern = epic.parent / where, pattern.format(ident)
    elif of is _LOGS:
        # A run from before sessions were kept: a directory of links per session.
        at, pattern = epic.parent / "sessions" / str(opened.get("name") or ""), f"*{ident}*.jsonl"
    else:
        return []
    try:
        return sorted(p for p in at.glob(pattern) if p.is_file())
    except (OSError, ValueError):
        return []


def _last_logged(epic: Path) -> int:
    """Epoch ms of the newest write to any of the run's records or session logs.
    The epic itself is written only when a session opens or the run ends, so a
    turn hours long leaves it untouched."""
    paths = _records(epic) + [p for o in _opened(epic) for p in _logs(epic, o)]
    newest = 0.0
    for p in paths:
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            pass
    return int(newest * 1000)


def _models(began: dict) -> str:
    """The distinct cli/model pairs the run's roles are on, e.g. "claude/claude-opus-5-5"."""
    seen: list[str] = []
    for a in began.get("agents") or []:
        if isinstance(a, dict) and a.get("model"):
            m = f"{a.get('backend')}/{a['model']}"
            if m not in seen:
                seen.append(m)
    return ", ".join(seen)


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


# ---- what a run spends ----
#
# hmz's status bar bills a run as it goes: the tokens each session's log reports,
# summed by kind and priced per model. The card reads the same logs the same way,
# and a run that is over carries hmz's own total, which wins.

#: The kinds of token hmz counts, under the names its price list prices them by.
_KINDS = ("input", "output", "cache_read", "cache_write")
#: What a Claude transcript calls each, on the `usage` of every assistant message.
_CLAUDE_USAGE = {"input": "input_tokens", "output": "output_tokens",
                 "cache_read": "cache_read_input_tokens",
                 "cache_write": "cache_creation_input_tokens"}

# A session log is appended to for hours. It is read from where the last poll
# left off, and what was found so far is kept here, per log.
_READ: dict[str, dict] = {}


def _spent_in(log: Path, backend: str) -> dict[str, dict[str, int]]:
    """Tokens by model, then by kind, spent in one session log so far."""
    key = str(log)
    st = _READ.get(key)
    try:
        size = log.stat().st_size
    except OSError:
        return st["spent"] if st else {}
    if st is None or size < st["pos"]:  # new, or rewritten shorter: start over
        st = _READ[key] = {"pos": 0, "spent": {}, "seen": set(), "model": ""}
    if size > st["pos"]:
        try:
            with log.open("rb") as f:
                f.seek(st["pos"])
                data = f.read()
        except OSError:
            return st["spent"]
        whole = data.rfind(b"\n") + 1  # a line still being written waits for the next poll
        fold = _fold_claude if backend == "claude" else _fold_codex
        for line in data[:whole].splitlines():
            try:
                d = json.loads(line)
            except Exception:
                continue
            if isinstance(d, dict):
                fold(st, d)
        st["pos"] += whole
    return st["spent"]


def _fold_claude(st: dict, d: dict) -> None:
    """Counts one transcript line: an assistant message's usage, once per message.
    Claude writes a line per content block, each carrying the whole message's."""
    msg = d.get("message") if d.get("type") == "assistant" else None
    if not isinstance(msg, dict):
        return
    usage, ident = msg.get("usage"), msg.get("id") or d.get("requestId")
    if not isinstance(usage, dict) or not ident or ident in st["seen"]:
        return
    st["seen"].add(ident)
    into = st["spent"].setdefault(str(msg.get("model") or ""), {})
    for kind, name in _CLAUDE_USAGE.items():
        n = usage.get(name)
        if isinstance(n, (int, float)) and n > 0:
            into[kind] = into.get(kind, 0) + int(n)


def _fold_codex(st: dict, d: dict) -> None:
    """Counts one rollout line: the running total Codex states after each
    response, on the model its turn context last named. Codex's input count
    includes the cached part, which is priced apart."""
    p = d.get("payload")
    if not isinstance(p, dict):
        return
    if d.get("type") == "turn_context" and p.get("model"):
        st["model"] = str(p["model"])
    elif d.get("type") == "event_msg" and p.get("type") == "token_count":
        total = (p.get("info") or {}).get("total_token_usage")
        if not isinstance(total, dict):
            return
        read = int(total.get("cached_input_tokens") or 0)
        kinds = {"input": int(total.get("input_tokens") or 0) - read, "cache_read": read,
                 "cache_write": int(total.get("cache_write_input_tokens") or 0),
                 "output": int(total.get("output_tokens") or 0)}
        st["spent"] = {st["model"] or "codex": {k: n for k, n in kinds.items() if n > 0}}


# The price list, indexed per file and kept until the file changes.
_PRICES: dict[str, tuple[float, dict]] = {}
_SPELLING = re.compile(r"[^a-z0-9]")
# Scraps that name a release rather than a model, cut the way hmz's own lookup
# cuts them: `claude-haiku-4-5-20251001` is priced as `claude-haiku-4.5`.
_DATED = re.compile(r"[-@_](?:19|20)\d{2}-?\d{2}-?\d{2}$")
_VERSIONED = re.compile(r"[-@]v\d+$")
_LATEST = re.compile(r"[-@](?:latest|stable)$")
_ROUTED = re.compile(r"^(?:bedrock|vertex|azure|aws|gcp)-")
_QUALIFIED = re.compile(r"^[a-z]+\.")


def _spelled(model: str) -> str:
    """One spelling of a model: the letters and digits two spellings share."""
    return _SPELLING.sub("", model.lower())


def _prices(home: Path) -> dict[str, dict[str, float]]:
    """hmz's copy of the price list — USD per million tokens by kind, under every
    spelling of each model it lists — or {} for a home without one."""
    path = home / "prices.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    kept = _PRICES.get(str(path))
    if kept and kept[0] == mtime:
        return kept[1]
    index: dict[str, dict[str, float]] = {}
    try:
        models = json.loads(path.read_text()).get("models") or {}
    except Exception:
        models = {}
    for ident, m in (models.items() if isinstance(models, dict) else []):
        per = m.get("per_million") if isinstance(m, dict) else None
        if not isinstance(per, dict):
            continue
        per = {k: float(v) for k, v in per.items() if isinstance(v, (int, float))}
        for name in (str(ident), str(m.get("name") or "")):
            if _spelled(name):
                index.setdefault(_spelled(name), per)
    _PRICES[str(path)] = (mtime, index)
    return index


def _price(model: str, prices: dict) -> Optional[dict[str, float]]:
    """The price of `model`, matched as hmz matches one: exact once the provider
    in front, the release behind and the punctuation are stripped. A near miss
    is a miss, and most models are not listed at all."""
    said = re.sub(r":\d+$", "", model.strip().lower())
    for whole in (said, said.rpartition("/")[2]):
        bare = _QUALIFIED.sub("", _QUALIFIED.sub("", whole))
        for one in (whole, bare, _ROUTED.sub("", bare)):
            for cut in (one, _LATEST.sub("", one), _VERSIONED.sub("", one), _DATED.sub("", one),
                        _DATED.sub("", _VERSIONED.sub("", _LATEST.sub("", one)))):
                found = prices.get(_spelled(cut))
                if found is not None:
                    return found
    return None


def _cost(kinds: dict[str, int], per: dict[str, float]) -> Optional[float]:
    """USD for `kinds` at `per` million, billed as hmz bills them: a cache write
    not priced on its own is an input token, a cache read not priced is left
    out, so the figure is never more than the truth. None when nothing was
    priced at all."""
    total, priced = 0.0, False
    for kind, n in kinds.items():
        rate = per.get(kind, per.get("input") if kind == "cache_write" else None)
        if rate is not None and n > 0:
            total, priced = total + n * rate / 1_000_000, True
    return total if priced else None


_ISO_DURATION = re.compile(r"^P(?:(\d+(?:\.\d+)?)D)?(?:T(?:(\d+(?:\.\d+)?)H)?"
                           r"(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?$")


def _seconds(duration) -> Optional[float]:
    """Seconds of a budget's `duration`: ISO 8601 as hmz writes it (`PT15H`,
    `P1DT2H30M`), or a bare number."""
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        return float(duration)
    m = _ISO_DURATION.match(str(duration or ""))
    if not m or not any(m.groups()):
        return None
    d, h, mi, s = (float(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _budget(began: dict) -> Optional[dict]:
    """The run's budget — `cost` (USD), `duration_s`, `output_tokens`, each None
    for no cap, and `graceful` — or None for a run that wrote none down."""
    b = began.get("budget")
    if not isinstance(b, dict):
        return None
    cost, out = b.get("cost"), b.get("output_tokens")
    # "Infinity" is how hmz writes no cap on money, the chat flow's budget.
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or math.isinf(cost):
        cost = None
    if isinstance(out, bool) or not isinstance(out, (int, float)):
        out = None
    return {"cost": float(cost) if cost is not None else None,
            "duration_s": _seconds(b.get("duration")),
            "output_tokens": int(out) if out is not None else None,
            "graceful": b.get("graceful") is not False}


def _money(dollars: float) -> str:
    """A bill as hmz's status bar writes one: cents, or four places under a cent."""
    if dollars >= 100:
        return f"${dollars:,.0f}"
    if dollars >= 0.01:
        return f"${dollars:.2f}"
    return f"${dollars:.4f}" if dollars > 0 else "$0.00"


def _thousands(count: float) -> str:
    """A token count as hmz's status bar writes one."""
    if count < 1000:
        return f"{count:.0f}"
    if count < 1_000_000:
        return f"{count / 1000:.1f}k"
    return f"{count / 1_000_000:.2f}M"


def _clock(seconds: float) -> str:
    """`45s`, `2m 5s`, `1h 12m`, `15h`: the largest two units, a zero one dropped."""
    s = int(seconds)
    for big, small, b, l in ((86400, 3600, "d", "h"), (3600, 60, "h", "m"), (60, 1, "m", "s")):
        if s >= big:
            rest = s % big // small
            return f"{s // big}{b}" + (f" {rest}{l}" if rest else "")
    return f"{s}s"


def _budget_label(budget: Optional[dict]) -> str:
    """What the budget caps, as hmz's own menu row words it: `15h, $150`, `no
    limit`, and `even mid-turn` for one that cuts a turn off rather than let it
    finish."""
    if budget is None:
        return ""
    caps: list[str] = []
    if budget["duration_s"] is not None:
        caps.append(_clock(budget["duration_s"]))
    if budget["output_tokens"] is not None:
        caps.append(f"{_thousands(budget['output_tokens'])} out")
    if budget["cost"] is not None:
        caps.append(_money(budget["cost"]))
    if not caps:
        return "no limit"
    return ", ".join(caps) + ("" if budget["graceful"] else ", even mid-turn")


def _spending(epic: Optional[Path], events: list[dict], home: Path,
              now: Optional[float] = None) -> dict:
    """What the run has spent, for the card: `cost` (USD; None while nothing is
    priced), `cost_floor` (some tokens went on a model the list has no price
    for), `tokens` by kind, `output_tokens`, `elapsed_s`, the `budget` and
    whether the run is `over_budget` — and `spend_label` / `spend_title` /
    `budget_label`, the words the card puts them in."""
    began, end = _began(events), _ended(events)
    usage = next((e for e in reversed(events) if e.get("event") == "usage"), None)
    prices = _prices(home)
    tokens: dict[str, int] = {}
    models: list[str] = []
    cost, priced, floor = 0.0, False, False
    for o in (_opened(epic) if epic else []):
        for log in _logs(epic, o) + _logs(epic, o, _SUBAGENT_LOGS):
            for model, kinds in _spent_in(log, str(o.get("backend") or "")).items():
                for kind, n in kinds.items():
                    tokens[kind] = tokens.get(kind, 0) + n
                if model and model not in models:
                    models.append(model)
                per = _price(model, prices) if prices else None
                billed = _cost(kinds, per) if per else None
                if billed is None:
                    floor = floor or any(kinds.values())
                else:
                    cost, priced = cost + billed, True
    out = tokens.get("output", 0)
    at = transcripts._parse_ts(str(began.get("at") or ""))
    elapsed: Optional[float] = None
    if usage:
        # hmz's own figures, written as the run ended: what it billed, whatever
        # the list prices, and the time its agents spent in turns.
        cost = float(usage.get("cost") or 0.0)
        priced, floor = cost > 0, False
        out = int(usage.get("output_tokens") or out)
        elapsed = float(usage.get("seconds") or 0.0)
    elif at and end:
        elapsed = max(0.0, transcripts._parse_ts(str(end.get("at") or "")) - at)
    elif at:
        elapsed = max(0.0, (time.time() if now is None else now) - at)
    budget = _budget(began)
    over = budget is not None and (
        (budget["cost"] is not None and priced and cost >= budget["cost"])
        or (budget["duration_s"] is not None and elapsed is not None
            and elapsed >= budget["duration_s"])
        or (budget["output_tokens"] is not None and out >= budget["output_tokens"]))
    parts: list[str] = []
    if priced:
        parts.append(_money(cost) + ("+" if floor else ""))
    if out:
        parts.append(f"{_thousands(out)} out")
    if elapsed is not None:
        parts.append(_clock(elapsed))
    title = " · ".join(f"{k} {_thousands(tokens[k])}" for k in _KINDS if tokens.get(k))
    if models:
        title += (" — " if title else "") + ", ".join(models)
    if floor:
        title += " · + a model the price list lacks: its tokens are counted, not billed"
    if usage:
        title = "hmz's own total, written as the run ended" + (" · " + title if title else "")
    return {"cost": cost if priced else None, "cost_floor": floor, "tokens": tokens,
            "output_tokens": out, "elapsed_s": elapsed, "budget": budget, "over_budget": over,
            "spend_label": " · ".join(parts), "spend_title": title,
            "budget_label": _budget_label(budget)}


def _current_task(events: list[dict], epic: Optional[Path] = None) -> str:
    """`<flow> · <agent at work>: <what its session is doing>`, or how the run
    ended. The agent at work is the one whose session log was written last —
    agents that take turns resume their sessions rather than open new ones —
    or, with no log to go by, the last to open one."""
    flow = _began(events).get("flow", "")
    end = _ended(events)
    if end:
        return f"{flow} {end.get('how', 'ended')}"
    opened = _opened(epic) if epic else [e for e in events if e.get("event") == "opened"]
    if not opened:
        return flow
    at_work, log, newest = opened[-1], None, -1.0
    for o in opened if epic else []:
        for p in _logs(epic, o):
            if _mtime(p) >= newest:
                at_work, log, newest = o, p, _mtime(p)
    task = f"{flow} · {at_work.get('agent', '')}"
    hint = ""
    if log and at_work.get("backend") == "claude":
        hint = transcripts.current_task_hint(log) or ""
    elif log and at_work.get("backend") == "codex":
        hint = codex._last_assistant_text(log)
    return f"{task}: {hint}" if hint else task


def list_hmz_windows() -> list[Window]:
    """Running hmz interfaces, one Window per tty, minus those the machine-local
    cwd filter hides. Linux-only (/proc)."""
    if not Path("/proc").is_dir():
        return []
    windows: list[Window] = []
    for pid, info in _proc_table().items():
        tty = info.get("tty", "")
        if not tty or tty in ("?", "??") or not _is_interactive_hmz(info.get("args", "")):
            continue
        if not _pid_alive(pid):
            continue
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            cwd = ""
        if not _cwd_visible(cwd):
            continue
        started_at = _proc_start_ms(pid)
        epic = _latest_epic(cwd, _home(pid))
        status, updated_at, session_id = "idle", started_at, f"hmz-{pid}"
        if epic:
            events = _events(epic)
            status = "busy" if _began(events) and not _ended(events) else "idle"
            updated_at = max(started_at, _last_logged(epic))
            session_id = epic.parent.name
        windows.append(Window(
            pid=pid,
            session_id=session_id,
            cwd=cwd,
            project_name=os.path.basename(cwd) or session_id,
            project_slug=_cwd_to_project_slug(cwd),
            name=None,
            status=status,
            waiting_for=None,
            started_at=started_at,
            updated_at=updated_at,
            version="",
            tty=get_tty(pid),
            transcript_path=str(epic) if epic else None,
            alive=True,
            hidden=False,
            platform="hmz",
        ))
    windows.sort(key=lambda w: (-w.updated_at, w.pid))
    return windows


def menu(w: Window) -> str:
    """Where in a menu hmz `w` stands — "parallel_flame_chase › Set budget for
    parallel_flame_chase" — or "" when it isn't in one or is running a flow."""
    if w.status == "busy" or not w.tty:
        return ""
    pane = tmux.pane_for_tty(w.tty)
    if pane is None:
        return ""
    lines = [l for l in tmux.capture_pane(pane).get("text", "").splitlines() if l.strip()]
    m = _CRUMB.match(lines[0]) if lines else None
    return m.group(1).strip() if m else ""


def hmz_window_dicts() -> list[dict]:
    """Live hmz windows as dashboard dicts, the shape codex_window_dicts gives.
    Shell-process counts are filled in by the caller."""
    out: list[dict] = []
    for w in list_hmz_windows():
        d = w.to_dict()
        epic = Path(w.transcript_path) if w.transcript_path else None
        events = _events(epic) if epic else []
        began, end = _began(events), _ended(events)
        current_task = _current_task(events, epic)
        tri = _classify_codex(w.status, d.get("idle_seconds", 0), current_task)
        crumb = menu(w)
        if crumb:
            # Not waiting_perm: that card's Quick Approve would type "1" into it.
            tri = {"triage": "stalled", "reason": f"停在菜单：{crumb}",
                   "suggestion": "去终端填完并 Save"}
        models = _models(began)
        d.update({
            "shell_proc_count": 0,
            "permission_msg": None,
            "permission_ts": None,
            "first_input": str(began.get("task") or "").strip().split("\n")[0][:100],
            "current_task": current_task or None,
            "last_error": f"{began.get('flow', 'run')} failed" if end.get("how") == "failed" else None,
            "triage": tri["triage"],
            "triage_reason": tri["reason"],
            "triage_suggestion": tri["suggestion"],
            "skills_used": [],
            "memory_ops": [],
            "background_tasks": [],
            "queued": [],
            "model": models,
            "effort": "",
            "model_label": models,
            "model_source": "transcript" if models else "",
            **_spending(epic, events, _home(w.pid)),
        })
        out.append(d)
    return out


def _history(path: Path, start: int = 0) -> list[dict]:
    """The lines in hmz's history.jsonl from byte `start` on, each {at, workdir, text}."""
    out: list[dict] = []
    try:
        with path.open("rb") as f:
            f.seek(start)
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return out
    for line in data.splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        if isinstance(d, dict) and isinstance(d.get("text"), str):
            out.append({"at": str(d.get("at") or ""), "workdir": str(d.get("workdir") or ""),
                        "text": d["text"]})
    return out


def _said(path: Path, start: int = 0) -> list[tuple[str, str]]:
    """(workdir, text) of each line in hmz's history.jsonl from byte `start` on."""
    return [(d["workdir"], d["text"]) for d in _history(path, start)]


def typed(pid: int, cwd: str, since_ms: int) -> list[dict]:
    """The lines typed into hmz `pid` since it started, oldest first, as hmz
    wrote them down — whether or not any of them started anything."""
    return [d for d in _history(_home(pid) / "history.jsonl")
            if d["workdir"] == cwd and transcripts._parse_ts(d["at"]) * 1000 >= since_ms]


def _squeeze(s: str) -> str:
    return "".join(s.split())


def prompt_taken(pid: int, cwd: str, text: str, pane: str) -> Callable[[], bool]:
    """A check that hmz `pid` took `text`, set up before it is pasted.

    An emptied composer proves nothing with hmz (see tmux.send_text_confirmed).
    What does: hmz writes every line it takes — a task, a word put into a
    running flow, a command — to <home>/history.jsonl before acting on it. It
    skips the line it was last given, though, so a repeat of that falls back to
    the screen: the line echoed above an emptied composer.
    """
    path = _home(pid) / "history.jsonl"
    try:
        mark = path.stat().st_size
    except OSError:
        mark = 0
    want = _squeeze(text)
    said = _said(path)
    # hmz's "last given" is the newest line typed in this directory, or the
    # newest anywhere when nothing was ever typed here.
    here = [t for where, t in said if where == cwd] or [t for _, t in said]
    if here and _squeeze(here[-1]) == want:
        return lambda: tmux._shown_above_composer(pane, text)
    return lambda: any(_squeeze(t) == want for _, t in _said(path, mark))


# hmz's own lines are the interface's, not an agent's, and each begins "hmz: ".
_REFUSAL = "hmz: "
_RULE = re.compile(r"^[─━\s]+$")


def refusal(pane: str, text: str) -> str:
    """What hmz said instead of acting on `text` — `hmz: no such flow: x` — or "".

    hmz writes a line down before reading it, so its history takes a line it then
    refuses: a `$flow` it doesn't have, a `/command` it doesn't know, a flow
    chosen while one runs. The refusal is only on its screen, the red line it
    puts right under the echo of what was typed (both above the composer).
    """
    lines = tmux.capture_pane(pane).get("text", "").splitlines()
    composer = next((i for i in range(len(lines) - 1, -1, -1)
                     if lines[i].lstrip().startswith("❯")), -1)
    needle = _squeeze(text)[-24:]
    if composer < 0 or not needle:
        return ""
    # Where the echo ends, found on the screen with its wrapping squeezed out.
    above = "\n".join(lines[:composer])
    at = [i for i, ch in enumerate(above) if not ch.isspace()]
    k = "".join(above[i] for i in at).rfind(needle)
    if k < 0:
        return ""
    said: list[str] = []
    for line in above[at[k + len(needle) - 1] + 1:].split("\n")[1:]:
        if not line.strip() or _RULE.match(line):
            if said:
                break
            continue
        if not said and not line.lstrip().startswith(_REFUSAL):
            return ""
        said.append(line.strip())
        if len(said) == 3:  # a long one wraps; the composer's own chrome follows it
            break
    return " ".join(said)


def _session_timeline(log: Path, backend: str, limit: int) -> list[dict]:
    if backend == "codex":
        return codex.codex_timeline(log, limit=limit)
    return transcripts.timeline(log, limit=limit)


def hmz_timeline(path: str | Path | None, limit: int = 60,
                 typed: list[dict] = ()) -> list[dict]:
    """A run as TurnEvent-compatible dicts: the task it began on, every turn of
    every session its agents opened — read from where the run keeps them, each
    tagged `extra.agent` with whose it was — each flow it called, and how it
    ended, in the order they happened.

    `typed` (see typed()) are the lines typed into the hmz; each one the run
    doesn't already show goes in where it was typed. A line hmz refused, or
    one typed before there was any run (`path` None), shows nowhere else."""
    epic = Path(path) if path else None
    events: list[dict] = []
    task = ""
    for e in _events(epic) if epic else []:
        kind, ts = e.get("event"), e.get("at", "")
        if kind == "began":
            task = _squeeze(cap_text(str(e.get("task") or ""), MESSAGE_CHARS))
            text = f"${e.get('flow', '')} {e.get('task', '')}".strip()
            events.append({"ts": ts, "kind": "user_text", "text": cap_text(text, MESSAGE_CHARS),
                           "tool": None, "role": "user", "extra": {}})
            continue
        if kind == "called":
            text = f"called flow {e.get('flow', '')}"
        elif kind == "returned":
            text = f"flow {e.get('flow', '')} returned"
        elif kind == "ended":
            text = f"run ended: {e.get('how', '')}"
        else:
            continue
        events.append({"ts": ts, "kind": "assistant_text", "text": text,
                       "tool": None, "role": "assistant", "extra": {}})
    # A forked session's log opens on a copy of the conversation it was cut
    # from, so a turn two sessions both hold is shown once.
    seen: set[tuple] = set()
    for o in _opened(epic) if epic else []:
        agent, backend = str(o.get("agent") or ""), str(o.get("backend") or "")
        said: list[dict] = []
        for log in _logs(epic, o):
            said += _session_timeline(log, backend, limit)
        if not said:
            # A CLI the board can't read, or a log that has gone: say the session
            # was opened, which is all the run itself knows.
            events.append({"ts": o.get("at", ""), "kind": "assistant_text",
                           "text": f"{agent} opened a {backend} session",
                           "tool": None, "role": "assistant", "extra": {"agent": agent}})
            continue
        for ev in said:
            key = (ev.get("ts"), ev.get("kind"), ev.get("tool"), ev.get("text"))
            if key in seen:
                continue
            seen.add(key)
            # The task the run began on, handed to its first agent word for
            # word, is already the run's own first line.
            if ev.get("kind") == "user_text" and task and _squeeze(ev.get("text") or "") == task:
                task = ""
                continue
            ev["extra"] = {**(ev.get("extra") or {}), "agent": agent}
            events.append(ev)
    # A typed line the run took is already here, as the task it began on or as
    # what an agent was told; hmz may wrap the latter, so it is looked for inside.
    shown = [_squeeze(ev.get("text") or "") for ev in events if ev.get("kind") == "user_text"]
    for d in typed:
        line = _squeeze(cap_text(d["text"], MESSAGE_CHARS))
        if line and not any(line in s for s in shown):
            events.append({"ts": d["at"], "kind": "user_text", "text": cap_text(d["text"], MESSAGE_CHARS),
                           "tool": None, "role": "user", "extra": {}})
    # Stable: the run's own lines keep their place among turns of the same instant.
    events.sort(key=lambda ev: transcripts._parse_ts(ev.get("ts") or ""))
    return events[-limit:]

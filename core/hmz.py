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

The run's course on the card — what it is on now, and the run's own lines on
its timeline — comes from that epic: what it began on, each session opened, each
flow it called and when that returned, how it ended. Beside it hmz keeps far
more: a record per called flow (epic.<flow>_<id>.jsonl), every session its agents
opened (sessions/), the engine's journal (resume.jsonl). A long run piles up
hundreds of those and tens of MB, and reading them all on every refresh once
kept a host's board from answering at all. The records and the journal are not
read. What the agents said is, but only the newest of it: the tails of the
session logs written last (see _said). An `opened` line is written once a
session's first turn has landed, so a flow an hour into its first turn has
nothing on the epic past `began`, while its log has been filling all along.

What a run spends is on the card too. hmz's status bar bills a run as it goes
off the logs its agents write — each turn's tokens by kind, priced per model
from its copy of openllmprices.com (see _price_list) — and the card reads the
same logs the same way while the run is on: every log under its sessions/,
<cli>/ laid out as that CLI lays out its home, each read on from where the last
poll stopped (see _spending). A run that is over carries hmz's own figures
instead, on its `usage` line: `cost` in USD, the `output_tokens` its agents
wrote, the `seconds` they spent in turns — and then no log is read at all. The
`budget` caps what a run may spend (`duration`, `cost`, `output_tokens`; the chat flow runs under
`cost: Infinity`, no cap), and its `graceful` says what happens to the turn
under way when a cap is hit: let finish and the next refused (the default), or
cut off at once — "even mid-turn", as hmz's own budget menu words it.

~/.hmz is $HUMANIZE_HOME when the hmz was started with one, as hmz's own
`home()` has it. It was ~/.humanize until hmz renamed it: a newer hmz moves the
old one over the first time it runs, and an older one goes on using it.

hmz's `/clear` clears its screen and nothing else: there is no context to clear,
a run's agents being made for that run, and every run stays where it was. So
the card, which reads the run rather than the screen, clears itself — from the
newest `/clear` on it shows only what came after (see cleared_at_ms).
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from . import codex, patrol, tmux, transcripts
from .sessions import (HOME_BASE, Window, _cwd_to_project_slug, _cwd_visible, _pid_alive,
                       _proc_cwd, _proc_start_ms, _ps_tty, proc_table)
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
# …and for one cleared since anything last happened in it.
CLEARED_NOTE = ("已 /clear。hmz 的 /clear 只清它自己的屏幕，卡片也就只显示这之后的内容。"
                "它没有上下文可清——每个 run 的 agent 都是为那个 run 新开的；之前的 run 没动，"
                "在 hmz 里 /epics 能看，/resume 能接着跑。")

# What the timeline says while hmz sits in a menu, which only its terminal can
# answer. A `$flow` its directory hasn't set up lands in one, holding the line.
MENU_NOTE = ("hmz 停在菜单「{}」上，要在它的终端里操作。第一次用某个 flow 时会先弹它的配置菜单"
             "（各角色用什么 agent、参数、预算），填完 Save 才会用你输入的那一行开跑。")

# …and while hmz asks something in a box over its screen, which it takes no line
# until it is answered: "Report errors to humanize?" on its first start, the
# save and leave questions on the way out of a menu.
QUESTION_NOTE = ("hmz 在问「{}」（{}），要在它的终端里回答。答之前它不收输入，"
                 "卡片发过去的也进不去。")

# The breadcrumb hmz tops each menu with — `hmz › parallel_flame_chase › Set
# budget for parallel_flame_chase` — and `● unsaved` beside it mid-edit.
_CRUMB = re.compile(r"^\s*hmz › (.+?)(?:\s{2,}●.*)?\s*$")

# hmz's question box (its Popup) has no breadcrumb: it is a bare ╭──╮ drawn over
# whatever was there, the question its first line and the keys that answer it
# its last — `enter yes   esc ask again next time`. A menu's own boxes (a sheet's
# rows) keep their keys outside them.
_BOX_TOP = re.compile(r"╭(─+)╮")
_ANSWER_KEYS = re.compile(r"^(?:enter \S+\s{2,})?esc \S")

# Commands that are not the interface: `hmz exec` runs a flow headless, and
# `hmz internal …` is the sandbox / credential plumbing under every turn.
_HEADLESS = {"exec", "internal"}
_PLAIN = re.compile(r"[^A-Za-z0-9]")


def _is_interactive_hmz(args: str) -> bool:
    """True for an `hmz` process that is its terminal interface."""
    toks = args.split()
    for i, t in enumerate(toks[:3]):
        if os.path.basename(t) == "hmz":
            return not any(a in _HEADLESS for a in toks[i + 1:])
    return False


def _environ(pid: int) -> dict[str, str]:
    """The environment hmz `pid` was started with, or {} where it can't be read."""
    try:
        env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return {}
    return dict(kv.decode(errors="replace").split("=", 1) for kv in env if b"=" in kv)


def _home(pid: int) -> Path:
    """Where hmz `pid` keeps its runs and its history."""
    v = _environ(pid).get("HUMANIZE_HOME")
    return Path(v) if v else _default_home()


def _default_home() -> Path:
    """~/.hmz once there is one — hmz uses it, and leaves the old one alone,
    from then on — else ~/.humanize, which an hmz from before the rename keeps."""
    return HMZ_HOME if HMZ_HOME.exists() or not HMZ_HOME_WAS.exists() else HMZ_HOME_WAS


def _latest_epic(cwd: str, home: Optional[Path] = None) -> Optional[Path]:
    """epic.jsonl of the newest run in `cwd`, or None before the first one."""
    runs = (home or _default_home()) / "epics" / _PLAIN.sub("-", cwd)
    try:
        names = sorted(os.listdir(runs), reverse=True)
    except OSError:
        return None
    return next((runs / n / "epic.jsonl" for n in names if (runs / n / "epic.jsonl").is_file()),
                None)


# The epic of a long run is tens of MB — every `called` line carries the task it
# handed over — and is read on every 2 s refresh and every timeline poll, though
# it changes only as a flow is called or returns.
@transcripts.memo_by_file
def _events(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        for d in transcripts._iter_lines(path):
            if isinstance(d, dict):
                out.append(d)
    except OSError:
        pass
    return out


def _began(events: list[dict]) -> dict:
    return next((e for e in events if e.get("event") == "began"), {})


def _ended(events: list[dict]) -> dict:
    return next((e for e in reversed(events) if e.get("event") == "ended"), {})


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


def _machine(pid: int) -> Path:
    """Where hmz `pid` keeps what is this machine's alone, as hmz's own
    `machine()` has it: humanize-<uid> in its temporary directory — the first
    of $TMPDIR, $TEMP and $TMP that is a directory, else /tmp, as Python's
    tempfile looks for one."""
    env = _environ(pid)
    tmp = next((v for v in (env.get(k) for k in ("TMPDIR", "TEMP", "TMP"))
                if v and os.path.isdir(v)), "/tmp")
    return Path(tmp) / f"humanize-{os.getuid()}"


def _price_list(pid: int) -> Path:
    """hmz `pid`'s copy of the price list. hmz keeps it in its machine's
    directory now (_machine) — a copy of what anybody can fetch again, nothing
    for the machines sharing a home to share — and kept it in its home before:
    the newer place wins where there is a list in both."""
    newer = _machine(pid) / "prices.json"
    older = _home(pid) / "prices.json"
    return older if not newer.is_file() and older.is_file() else newer


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

# Where each CLI logs a session under the run's own directory for it: Claude's
# projects/<dir>/<id>.jsonl, and the transcript a sub-agent it starts writes under
# that session's directory (hmz bills it as the run's); Codex's rollouts.
_BILLED = {"claude": "sessions/claude/projects/**/*.jsonl",
           "codex": "sessions/codex/sessions/**/rollout-*.jsonl"}


def _billed(epic: Path) -> list[tuple[Path, str]]:
    """Every session log the run keeps, with the CLI that wrote it."""
    out: list[tuple[Path, str]] = []
    for backend, pattern in _BILLED.items():
        try:
            out += [(p, backend) for p in sorted(epic.parent.glob(pattern)) if p.is_file()]
        except (OSError, ValueError):
            continue
    return out


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


# Indexed once per version of the file.
@transcripts.memo_by_file
def _prices(path: Path) -> dict[str, dict[str, float]]:
    """hmz's copy of the price list at `path` (see _price_list) — USD per
    million tokens by kind, under every spelling of each model it lists — or {}
    where there is none."""
    index: dict[str, dict[str, float]] = {}
    try:
        models = json.loads(Path(path).read_text()).get("models") or {}
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


# Where a running run's spend label names how long it has run. Its clock is not
# written in: it changes every second, and the board would re-send the whole
# snapshot every 2s for as long as the run goes (see _TICKING_CARD in app.py).
# The page puts it in from the card's elapsed_s, advanced on its own clock, in
# _clock's words (cardText in index.html). A run that is over keeps its clock
# in the label, since it no longer moves.
ELAPSED = "{elapsed}"


def _clock(seconds: float) -> str:
    """`45s`, `2m 5s`, `1h 12m`, `15h`: the largest two units, a zero one dropped.
    The page words a running run's clock the same way (runClock in index.html)."""
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


def _spending(epic: Optional[Path], events: list[dict], prices_at: Path,
              now: Optional[float] = None) -> dict:
    """What the run has spent, for the card: `cost` (USD; None while nothing is
    priced), `cost_floor` (some tokens went on a model the list has no price
    for), `tokens` by kind, `output_tokens`, `elapsed_s`, the `budget` and
    whether the run is `over_budget` — and `spend_label` / `spend_title` /
    `budget_label`, the words the card puts them in (a running run's clock as
    ELAPSED, for the page to fill in from `elapsed_s`)."""
    began, end = _began(events), _ended(events)
    usage = next((e for e in reversed(events) if e.get("event") == "usage"), None)
    prices = _prices(prices_at)
    tokens: dict[str, int] = {}
    models: list[str] = []
    cost, priced, floor = 0.0, False, False
    # Once the run has ended its own total is on the `usage` line: no log to read.
    for log, backend in (_billed(epic) if epic and not usage else []):
        for model, kinds in _spent_in(log, backend).items():
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
    running = False
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
        running = True
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
        parts.append(ELAPSED if running else _clock(elapsed))
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

def _current_task(events: list[dict]) -> str:
    """`<flow> · <what it is on>`, off the run's own lines: the flows it called
    that have not returned (`lane_turn ×3` for three of one at once), else the
    agent that opened a session last — or how the run ended."""
    flow = _began(events).get("flow", "")
    end = _ended(events)
    if end:
        return f"{flow} {end.get('how', 'ended')}"
    calls: dict[str, str] = {}  # each call not yet returned: its record → the flow
    agent = ""
    for e in events:
        kind = e.get("event")
        if kind == "called":
            calls[str(e.get("epic") or e.get("flow"))] = str(e.get("flow") or "")
        elif kind == "returned":
            calls.pop(str(e.get("epic") or e.get("flow")), None)
        elif kind == "opened":
            agent = str(e.get("agent") or "")
    out: dict[str, int] = {}
    for called in calls.values():
        out[called] = out.get(called, 0) + 1
    on = ", ".join(f + (f" ×{n}" if n > 1 else "") for f, n in out.items()) or agent
    return f"{flow} · {on}" if on else flow


def list_hmz_windows() -> list[Window]:
    """Running hmz interfaces, one Window per tty, minus those the machine-local
    cwd filter hides. Linux-only (/proc)."""
    return [w for w, _ in _discover()]


def _discover() -> list[tuple[Window, list[dict]]]:
    """list_hmz_windows, each window with its latest run's events — read once,
    for the card's status and time here and its details in hmz_window_dicts."""
    if not Path("/proc").is_dir():
        return []
    windows: list[tuple[Window, list[dict]]] = []
    for pid, info in proc_table().items():
        tty = _ps_tty(info.tty)
        if not tty or not _is_interactive_hmz(info.args):
            continue
        if not _pid_alive(pid):
            continue
        cwd = _proc_cwd(pid)
        if not _cwd_visible(cwd):
            continue
        started_at = _proc_start_ms(pid)
        epic = _latest_epic(cwd, _home(pid))
        status, updated_at, session_id = "idle", started_at, f"hmz-{pid}"
        events: list[dict] = []
        if epic:
            events = _events(epic)
            status = "busy" if _began(events) and not _ended(events) else "idle"
            # The epic is written as sessions open and flows are called and
            # return; a long turn leaves it untouched, and `busy` says enough.
            updated_at = max(started_at, int(_mtime(epic) * 1000))
            session_id = epic.parent.name
        # A line typed into it happens in it too, a /clear among them, and
        # hmz writes no run for one that starts none: the card is idle from
        # the newest of those, the board's stamp of a /clear it sent
        # (codex.mark_cleared) included, or from the run's own last write.
        updated_at = max([updated_at, codex.cleared_at_ms(pid)]
                         + [int(transcripts._parse_ts(d["at"]) * 1000)
                            for d in typed(pid, cwd, started_at)])
        windows.append((Window(
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
            tty=tty,
            transcript_path=str(epic) if epic else None,
            alive=True,
            hidden=False,
            platform="hmz",
        ), events))
    windows.sort(key=lambda found: (-found[0].updated_at, found[0].pid))
    return windows


def _screen(w: Window) -> list[str]:
    """The non-blank lines on hmz `w`'s screen, or none while it runs a flow."""
    if w.status == "busy":
        return []
    return [l for l in (tmux.capture_tty(w.tty) or "").splitlines() if l.strip()]


def _crumb(lines: list[str]) -> str:
    m = _CRUMB.match(lines[0]) if lines else None
    return m.group(1).strip() if m else ""


def _question(lines: list[str]) -> tuple[str, str]:
    """What the question box on `lines` asks and the keys that answer it —
    ("Report errors to humanize?", "enter yes · esc ask again next time") —
    or ("", "") when there is none."""
    for i, top in enumerate(lines):
        for m in _BOX_TOP.finditer(top):
            rule = m.group(1)
            # Its rows by its width, not its column: what is drawn left of the
            # box can hold wide characters, and the box's own │ are the pair
            # exactly that far apart.
            row = re.compile(rf"│(.{{{len(rule)}}})│")
            said = []
            for line in lines[i + 1:]:
                if f"╰{rule}╯" in line:
                    break
                r = row.search(line)
                if r and r.group(1).strip():
                    said.append(r.group(1).strip())
            if len(said) >= 2 and _ANSWER_KEYS.match(said[-1]):
                return said[0], re.sub(r"\s{2,}", " · ", said[-1])
    return "", ""


def question(w: Window) -> tuple[str, str]:
    """What hmz `w` is asking in a box over its screen, and the keys that answer
    it, or ("", "") — see _question."""
    return _question(_screen(w))


def held(w: Window) -> tuple[str, str, str]:
    """What holds hmz `w` on its own terminal, off one read of its screen: the
    question it asks and the keys that answer it (see _question), else where in
    a menu it stands — "parallel_flame_chase › Set budget for
    parallel_flame_chase" — as (asked, keys, crumb). The box over a menu comes
    first; all "" when neither, or while it runs a flow."""
    lines = _screen(w)
    asked, keys = _question(lines)
    return asked, keys, "" if asked else _crumb(lines)


def _held_note(w: Window) -> Optional[str]:
    """What the timeline says while hmz `w` waits on its own terminal (see
    held), or None."""
    asked, keys, crumb = held(w)
    if asked:
        return QUESTION_NOTE.format(asked, keys)
    return MENU_NOTE.format(crumb) if crumb else None


def live_timeline(w: Window, limit: int) -> dict:
    """A live hmz card's timeline: its `events` (see hmz_timeline), with what was
    typed into it too — hmz takes a line it then refuses, and that line is in
    no run — and the `note` that says what holds it or why there is nothing."""
    lines = typed(w.pid, w.cwd, w.started_at)
    cleared = cleared_at_ms(lines, codex.cleared_at_ms(w.pid))
    events = hmz_timeline(w.transcript_path, limit=limit, typed=lines, since_ms=cleared)
    note = _held_note(w)
    if note is None and cleared and not events:
        note = CLEARED_NOTE
    elif note is None and not w.transcript_path:
        note = TYPED_NO_RUN_NOTE if lines else NO_RUN_NOTE
    return {"events": events, "note": note}


def hmz_window_dicts() -> list[dict]:
    """Live hmz windows as dashboard dicts, the shape codex_window_dicts gives.
    Shell-process counts are filled in by the caller."""
    out: list[dict] = []
    for w, events in _discover():
        d = w.to_dict()
        began, end = _began(events), _ended(events)
        first_input = str(began.get("task") or "").strip().split("\n")[0][:100]
        current_task = _current_task(events)
        last_error = f"{began.get('flow', 'run')} failed" if end.get("how") == "failed" else None
        epic = Path(w.transcript_path) if w.transcript_path else None
        cleared = cleared_at_ms(typed(w.pid, w.cwd, w.started_at), codex.cleared_at_ms(w.pid))
        if end and _before(end.get("at", ""), cleared):
            # Over by the clear, so off hmz's screen and off the card, its bill
            # with it: a cleared card starts again from nothing, as a cleared
            # Claude card does. hmz keeps the run, under /epics. A run still
            # going stays on its screen, and on the card. The models stay, as
            # they do on the lines round hmz's composer.
            first_input, current_task, last_error = "", "", None
            epic, events = None, []
        tri = patrol.classify_idle(w.status, d.get("idle_seconds", 0), current_task)
        asked, keys, crumb = held(w)
        # Not waiting_perm: that card's Quick Approve would type "1" into it.
        if asked:
            tri = patrol._triage("stalled", f"在问：{asked}", f"去终端回答（{keys}）")
        elif crumb:
            tri = patrol._triage("stalled", f"停在菜单：{crumb}", "去终端填完并 Save")
        models = _models(began)
        d.update({
            "permission_msg": None,
            "first_input": first_input,
            "current_task": current_task or None,
            "last_error": last_error,
            **tri,
            "skills_used": [],
            "memory_ops": [],
            "background_tasks": [],
            "queued": [],
            "model": models,
            "effort": "",
            "model_label": models,
            "model_source": "transcript" if models else "",
            **_spending(epic, events, _price_list(w.pid)),
        })
        out.append(d)
    return out


# Its first start asks "Report errors to humanize?" in a box over the composer,
# and an hmz the board spawned takes nothing until somebody answers it in its
# terminal. So the board answers yes before it spawns one — only where nobody
# has answered yet, so a no given since in /settings stays a no — by running
# hmz's own Settings in hmz's own Python, the write the box's yes makes:
# $HUMANIZE_HOME, the lock, and the move from ~/.humanize stay hmz's business.
_ANSWER_REPORTS = ("from hmz.runtime.settings import Settings\n"
                   "s = Settings()\n"
                   "if s.enable_sentry is None:\n"
                   "    s.answers(enable_sentry=True)\n")
_ANSWER_TIMEOUT = 15


def _python_of(script: str) -> Optional[str]:
    """The Python on the #! line of the `hmz` script `script`, or None."""
    try:
        with open(script, "rb") as f:
            first = f.readline(1024).decode("utf-8", "replace")
    except OSError:
        return None
    argv = first[2:].split() if first.startswith("#!") else []
    if argv and os.path.basename(argv[0]) == "env":
        argv = [a for a in argv[1:] if not a.startswith("-")]
        return shutil.which(argv[0], path=tmux._spawn_env().get("PATH")) if argv else None
    return argv[0] if argv else None


def answer_reports(exe: str, cwd: str) -> bool:
    """Answer yes, for every hmz of `exe`'s home, to the error-report question
    where it hasn't been answered. False when that couldn't be done, which only
    leaves the question for the terminal, as it was."""
    python = _python_of(exe)
    if python is None:
        return False
    try:
        r = subprocess.run([python, "-c", _ANSWER_REPORTS], cwd=cwd, env=tmux._spawn_env(),
                           stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=_ANSWER_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


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


# Every hmz on a home shares its history, which grows for as long as hmz is used
# and is read for every hmz card on every refresh: read once per version.
_all_history = transcripts.memo_by_file(_history)


def typed(pid: int, cwd: str, since_ms: int) -> list[dict]:
    """The lines typed into hmz `pid` since it started, oldest first, as hmz
    wrote them down — whether or not any of them started anything."""
    return [d for d in _all_history(_home(pid) / "history.jsonl")
            if d["workdir"] == cwd and transcripts._parse_ts(d["at"]) * 1000 >= since_ms]


def _is_clear(text: str) -> bool:
    """True for a line hmz runs as `/clear`, which it reads as a name up to
    the first space."""
    return text.startswith("/") and text[1:].partition(" ")[0] == "clear"


def cleared_at_ms(typed: list[dict], stamped_ms: int = 0) -> float:
    """When the hmz `typed` (see typed()) was typed into last cleared its
    screen, in epoch ms, or 0 if it never did.

    That is the newest `/clear` among the lines, or `stamped_ms`, the board's
    stamp of a /clear it sent (codex.mark_cleared), whichever is later: hmz
    doesn't write down a repeat of its last line, so a second /clear in a row
    is in the stamp alone. In the lines' own float ms, so that the /clear line
    itself falls at the cutoff rather than a fraction of a ms after it.
    """
    return max([float(stamped_ms)] + [transcripts._parse_ts(d["at"]) * 1000
                                      for d in typed if _is_clear(d["text"])])


def _before(ts: str, cleared_ms: float) -> bool:
    """True if a line stamped `ts` is gone from the screen a clear at
    `cleared_ms` cleared — the /clear itself included. A stamp that doesn't
    parse is kept: better a stray line than a card blanked on a guess."""
    t = transcripts._parse_ts(ts) * 1000
    return 0 < t <= cleared_ms


def _squeeze(s: str) -> str:
    return "".join(s.split())


# The glyph hmz's composer line opens with (see tmux.COMPOSER_MARKERS).
_COMPOSER = tmux.composer_marker("hmz")


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
    said = _all_history(path)
    # hmz's "last given" is the newest line typed in this directory, or the
    # newest anywhere when nothing was ever typed here.
    here = [d["text"] for d in said if d["workdir"] == cwd] or [d["text"] for d in said]
    if here and _squeeze(here[-1]) == want:
        if _is_clear(text):
            # …but a /clear wipes its own echo with the rest of the screen.
            # The composer letting go of it is all there is to go on, and all
            # a lost one would leave is a screen that wasn't cleared twice.
            return lambda: not tmux._composer_has_tail(pane, text, _COMPOSER)
        return lambda: tmux._shown_above_composer(pane, text, _COMPOSER)
    return lambda: any(_squeeze(d["text"]) == want for d in _history(path, mark))


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
                     if lines[i].lstrip().startswith(_COMPOSER)), -1)
    needle = tmux._needle(text)
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


# What the agents said, for the timeline: each CLI's own sessions under the
# run's directory for it. Not Claude's sub-agents (projects/*/<id>/subagents/),
# billed as the run's but not the agents the flow drove.
_SAID = ("sessions/claude/projects/*/*.jsonl", "sessions/codex/sessions/**/rollout-*.jsonl")
# How much of each log: its newest this many events.
_SAID_TAIL = 200


@transcripts.memo_by_file
def _said_in(log: Path) -> list[dict]:
    """The newest of what one session log says, as timeline events. Kept until
    the log grows: a long run's older sessions are done with and never change."""
    if "/sessions/codex/" in str(log):
        if codex._is_subagent_rollout(str(log)):
            return []
        return codex.codex_timeline(log, limit=_SAID_TAIL, tail=_SAID_TAIL * 2)
    return transcripts.timeline(log, limit=_SAID_TAIL)


def _said(epic: Path, events: list[dict], limit: int) -> list[dict]:
    """What the run's agents said, newest `limit` events or so, each tagged
    `extra.agent` with whose it was where an `opened` line says.

    Off the logs written last, until what is found covers everything the rest
    could add: a log's events are none of them newer than the log, so once
    `limit` are in hand from after the next log was last written, it and every
    log older than it are left unread. A run of hundreds of sessions has a few
    going at once; the others' tails are kept (_said_in).

    What hmz handed an agent in its turn is `meta`: nobody typed it, and it is
    no prompt of the person's (lastUserPrompt on the page)."""
    whose = {str(e["session"]): str(e.get("agent") or "") for e in events
             if e.get("event") == "opened" and e.get("session")}
    logs: list[tuple[float, Path]] = []
    for pattern in _SAID:
        try:
            logs += [(_mtime(p), p) for p in epic.parent.glob(pattern) if p.is_file()]
        except (OSError, ValueError):
            continue
    logs.sort(key=lambda found: -found[0])
    out: list[dict] = []
    for mtime, log in logs:
        if len(out) >= limit and mtime < transcripts._parse_ts(out[-limit]["ts"] or ""):
            break
        agent = next((a for ident, a in whose.items() if a and log.stem.endswith(ident)), "")
        for ev in _said_in(log):
            extra = dict(ev.get("extra") or {})
            if agent:
                extra["agent"] = agent
            if ev.get("kind") == "user_text":
                extra["meta"] = True
            out.append({**ev, "extra": extra})
        out.sort(key=lambda ev: transcripts._parse_ts(ev.get("ts") or ""))
    return out[-limit:]


def hmz_timeline(path: str | Path | None, limit: int = 60,
                 typed: list[dict] = (), since_ms: float = 0) -> list[dict]:
    """A run as TurnEvent-compatible dicts: off its epic, the task it began on,
    each session an agent opened (tagged `extra.agent` with whose), each flow it
    called and its return, and how it ended; and the newest of what its agents
    said in their sessions (see _said) — in the order they happened.

    `typed` (see typed()) are the lines typed into the hmz; each one the run
    doesn't already show goes in where it was typed. A line hmz refused, or
    one typed before there was any run (`path` None), shows nowhere else.

    `since_ms` (see cleared_at_ms) drops what a /clear took off hmz's screen."""
    events: list[dict] = []
    run = _events(Path(path)) if path else []
    for e in run:
        kind, ts, extra = e.get("event"), e.get("at", ""), {}
        if _before(ts, since_ms):
            continue
        if kind == "began":
            text = f"${e.get('flow', '')} {e.get('task', '')}".strip()
            events.append(transcripts.event(ts, "user_text", cap_text(text, MESSAGE_CHARS),
                                            role="user"))
            continue
        if kind == "opened":
            agent = str(e.get("agent") or "")
            text, extra = f"{agent} opened a {e.get('backend', '')} session", {"agent": agent}
        elif kind == "called":
            text = f"called flow {e.get('flow', '')}"
        elif kind == "returned":
            text = f"flow {e.get('flow', '')} returned"
        elif kind == "ended":
            text = f"run ended: {e.get('how', '')}"
        else:
            continue
        events.append(transcripts.event(ts, "assistant_text", text, role="assistant",
                                        extra=extra))
    # A typed line the run took is already here, in the line it began on —
    # `$<flow> <task>`, so a line typed without its `$<flow>` is looked for inside.
    shown = [_squeeze(ev.get("text") or "") for ev in events if ev.get("kind") == "user_text"]
    for d in typed:
        line = _squeeze(cap_text(d["text"], MESSAGE_CHARS))
        if line and not _before(d["at"], since_ms) and not any(line in s for s in shown):
            events.append(transcripts.event(d["at"], "user_text",
                                            cap_text(d["text"], MESSAGE_CHARS), role="user"))
    if path:
        events += [ev for ev in _said(Path(path), run, limit) if not _before(ev.get("ts") or "", since_ms)]
    # Stable: at one instant the run's own lines come first, in their order.
    events.sort(key=lambda ev: transcripts._parse_ts(ev.get("ts") or ""))
    return events[-limit:]

English | [中文](README.zh-CN.md)

# Claude Fleet

When you're vibe coding with 5–7 Claude Code and Codex windows open at once, you
need one place to see what every window is doing — who's stuck, who's waiting on
you, who's done — and to act on them without hunting for the right terminal tab.

Both **Claude Code** and **Codex** sessions show up as live cards, each tagged
with its agent (a blue `cc` or green `codex` badge) so you can tell them apart at
a glance.

![](docs/screenshot-hero.png)

## Run it in 30 seconds

```bash
git clone https://github.com/LukeLIN-web/claude-board
cd claude-board && bash run.sh
# open http://127.0.0.1:7878 in your browser
```

The first run creates a venv and installs dependencies automatically — nothing to
set up. Change the port with `CLAUDE_FLEET_PORT=9000 bash run.sh`.

## What it solves

The everyday pain of multi-window vibe coding:

- **Permission prompts flash by and you miss them** → a persistent red bar at the top; click it to jump back to that terminal.
- **You don't know what each window is doing** → every card shows the current task, triage status, and background jobs.
- **Finished windows get left open** → the patrol engine marks them `closeable`; close any session with one click.
- **Switching terminals to type one line is tedious** → spawn a new session, or send a one-off prompt, straight from the dashboard (Linux + tmux).
- **You can't find that session from last week** → full-text search returns in ~50ms with VS Code–style match context.
- **You don't know how much a skill actually gets used** → 3-dimensional stats (invokes + file read/write + bash references).
- **You don't know who touched a memory** → in-degree (↓ sessions that read it) + out-degree (↑ sessions that wrote it).

## Core features

### Triage classification

Not a simple busy/idle flag. The patrol engine reads each transcript's
`stop_reason`, `queue-operation` events, and background-task state:

| Status | Meaning | How it's decided |
|--------|---------|------------------|
| 🟢 working | actively working | busy, or has a live Monitor/Bash background task |
| 🔴 waiting | waiting on you | permission prompt / dialog open |
| 🟡 stalled | stuck | stop_reason=tool_use + idle > 5 min |
| 🔵 completed | done | stop_reason=end_turn + idle > 5 min |
| ⚪ closeable | safe to close | completed + idle > 1 h |

Background tasks (`Bash run_in_background`, `Monitor persistent`) are tracked by
pairing tool_use/tool_result; finished ones are cleared automatically, so they
don't get misread as `working`.

### Search

ripgrep across all Claude + Codex transcripts, ~50ms. It doesn't just search
session titles — searching "hailuo" finds a session that mentioned Hailuo in the
conversation, even if the title is "you should check klingai.com".

Each result carries up to 3 match-context snippets so you can see at a glance why
it matched.

![](docs/screenshot-search.png)

### Skill / memory tracking

The skill panel reports three dimensions:

```
paper2video        333   1 invoke · ↓122 reads · ↑53 writes · 157 bash
feishu-notify       45  24 invokes · ↓7 reads · ↑7 writes · 7 bash
qzcli-topdowneval   12   3 invokes · ↓1 reads · ↑2 writes · 6 bash
```

If you only counted formal `/skill-name` invocations you'd get 44; adding
Read/Write/Edit of skill files plus Bash references to `skills/` brings the real
total to 431.

The memory panel groups by type (user / feedback / project / reference) and shows
`↓3 ↑2` per entry (read by 3 sessions, modified by 2).

![](docs/screenshot-skills.png)
![](docs/screenshot-memory.png)

### Timeline + plan history

Open any session to see the full conversation flow, opened scrolled to the most
recent event. Skill calls are purple, memory reads are dashed blue, memory writes
are pink.

Plan version history: a session typically iterates on its plan 5–14 times — each
Write is a full snapshot, each Edit is a red/green diff.

![](docs/screenshot-timeline.png)

### Spawn & send (Linux + tmux)

Claude Fleet is read-only by default, but two opt-in, tmux-backed controls let you
drive sessions without leaving the dashboard. They appear only when tmux is
available.

- **Spawn a session** — pick the agent (**Claude Code** or **Codex**) and a recent
  directory (or type one) in the header, then hit **Spawn**. Fleet runs
  `tmux new-window … claude --dangerously-skip-permissions` or
  `tmux new-window … codex --yolo` so the new session starts fully non-interactive —
  no permission prompts blocking the pane. The new window shows up on the next 2s poll.
- **Send a prompt** — each card has a `Send a prompt…` box. Type a line, press
  Enter, and Fleet injects it into that session's tmux pane via
  `tmux send-keys` (literal text + a separate Enter to submit).

> `--dangerously-skip-permissions` (Claude) / `--yolo` (Codex) auto-approve
> everything in a spawned session. It's the right trade-off for driving your own
> sessions locally — just don't spawn in directories you don't trust.

> **How Codex sessions are discovered.** Codex doesn't write a pid-keyed session
> file like Claude does, so Fleet finds live Codex TUIs from running processes
> (grouped by their controlling tty) and maps each to its `rollout-*.jsonl`
> transcript via `/proc/<pid>/fd` once the first turn opens it. A freshly spawned
> Codex still appears as a card immediately; its session id / transcript fill in
> after the first turn. (Linux only; background `codex mcp-server` / `app-server`
> processes are excluded.)

### Actions

| Button | What it does |
|--------|--------------|
| Focus | jump to that terminal tab |
| Timeline | expand the full conversation timeline + plan history |
| Send | inject a single-line prompt into the session's tmux pane (Linux + tmux) |
| Fork | `claude --resume <sid> --fork-session` — new session inherits the history |
| Resume | `claude --resume <sid>` — continue the original session (from the history list) |
| Review | send `/humanize:ask-codex review` into the session (Linux + tmux) |
| Close | SIGTERM — available on every card |
| Export | export a conversation doc (timeline + plan history + skill/memory summary) |

On **Codex** cards the platform-agnostic controls (Close, Send a prompt, Esc,
Commit) work the same way; the Claude-specific ones (Fork, Review, Clear, and the
permission quick-approve) are hidden, since they rely on Claude slash commands or
the `claude` binary.

> **Focus setup (macOS).** Focus works out of the box on Terminal.app and iTerm2 —
> including when your sessions run inside **tmux** (the bundled
> [`scripts/focus-tty.sh`](scripts/focus-tty.sh) maps the process tty → the owning
> terminal tab → raises it). To customize for another terminal or window manager,
> drop an executable `~/.claude/focus-tty.sh` taking a `<tty>` arg; it takes
> precedence over the bundled default.

## Architecture

Single-file frontend (Alpine.js + Tailwind via CDN — no npm). The Python backend
never writes to the stored harness data under `~/.claude/` and `~/.codex/` — that
data stays read-only. It is read-only **by default**: a few explicit,
user-triggered actions (fork, close, and the tmux-backed session spawn /
single-prompt injection on Linux, including the Clear/Commit/Review prompt
shortcuts) act on live sessions, never on the stored data.

```
app.py                FastAPI + SSE (2s polling)
core/
  sessions.py         read sessions/*.json, map to TTY (Window + platform field)
  transcripts.py      parse JSONL; extract skill/memory/plan/background tasks
  patrol.py           triage classification engine
  codex.py            Codex session parsing + live-session discovery (/proc + fd)
  search.py           cross-platform ripgrep search
  actions.py          focus / fork / close / export / spawn / send-prompt
  tmux.py             tmux backend: spawn window + inject prompt (Linux)
  history.py          unified index + full-text rg search
  skills.py           skill directory scan
  memory.py           memory file parsing
  plans.py            plan association (extracted from transcripts)
  perms.py            permission events
static/index.html     single-file SPA
```

## Acknowledgements

- [HarnessKit](https://github.com/RealZST/HarnessKit) — UI reference for cross-platform skill management
- [Synergy](https://github.com/SII-Holos/synergy) — inspiration for the memory-engram classification view

## License

[MIT](LICENSE)

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
Commit, Clear) work the same way — Codex has its own `/clear` ("clear the
terminal and start a new chat"), so the same command serves both. The
Claude-specific controls (Fork, Review, and the permission quick-approve) are
hidden, since they rely on Claude slash commands or the `claude` binary.

### Locate a session by id

External tools (overseer skills, scripts, you reading a transcript filename)
usually hold a *session id*, not a pid or pane. Two equivalent reverse lookups
resolve `session id → pid → tty → tmux pane`:

- **API** — `GET /api/locate/<session-id>` (a unique prefix of ≥ 8 chars works
  too) returns the window plus `tmux_pane` / `tmux_target`. Covers live Claude
  *and* Codex sessions.
- **Standalone** — [`scripts/locate-session.sh`](scripts/locate-session.sh)
  does the same for Claude sessions with just bash+jq+tmux, no server needed:

  ```console
  $ scripts/locate-session.sh 8ce5b822
  {"session_id":"8ce5b822-…","pid":116440,"tty":"/dev/pts/7","tmux_pane":"%3","tmux_target":"j1:2.0",…}
  ```

Both lean on the fact that Claude Code registers every live session in
`~/.claude/sessions/<pid>.json` (`{pid, sessionId, cwd, …}`), so the mapping is
a lookup, not a heuristic.

> **Focus setup (macOS).** Focus works out of the box on Terminal.app and iTerm2 —
> including when your sessions run inside **tmux** (the bundled
> [`scripts/focus-tty.sh`](scripts/focus-tty.sh) maps the process tty → the owning
> terminal tab → raises it). To customize for another terminal or window manager,
> drop an executable `~/.claude/focus-tty.sh` taking a `<tty>` arg; it takes
> precedence over the bundled default.

### Remote access

The server binds loopback only. Two ways to reach it from a phone, differing in
*where the login lives*.

> **The login is load-bearing, whichever you pick.**
> `POST /api/windows/<pid>/keys` types into a tmux pane, so an unprotected
> tunnel is a public shell on a hostname that gets scanned. Both scripts below
> refuse to publish a board they cannot prove is protected.

**A fixed domain, login at the edge** — [`scripts/tunnel.sh`](scripts/tunnel.sh)
publishes on a reserved [ngrok](https://ngrok.com) domain behind a Google
sign-in:

```console
$ scripts/tunnel.sh start          # also: stop | status
[tunnel] up -> https://<your-domain>.ngrok-free.dev (Google sign-in required)
```

Set `FLEET_TUNNEL_DOMAIN` / `FLEET_TUNNEL_ALLOWED_EMAILS` in `.env.local`, and
store the authtoken with `ngrok config add-authtoken` — none of it belongs in
the repo. The script generates the ngrok traffic policy itself and refuses to
start without one. It takes two rules: a Google sign-in *and* an allow-list
check, or every Google account on earth would qualify.

**A free random URL, login in the app** — [`scripts/cf-tunnel.sh`](scripts/cf-tunnel.sh)
uses a [Cloudflare](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/)
quick tunnel, which needs no account and no domain:

```console
$ scripts/cf-tunnel.sh start       # also: stop | status | url
[cf-tunnel] up -> https://<random-words>.trycloudflare.com (password required)
```

A quick tunnel has no edge policy at all, so here the gate is the app's own
password ([`core/auth.py`](core/auth.py)): set `FLEET_AUTH_PASSWORD` in
`.env.local` and restart the board. The script *probes* the running server and
refuses to publish unless an anonymous request is actually rejected — it does
not just read the variable, because `run.sh` runs uvicorn detached and a
password added after startup is set in your shell and absent in the process.
The URL is random and changes on every start; `cf-tunnel.sh url` reprints it.

The two are independent. The password gate works behind either tunnel, or
behind none — it is on whenever `FLEET_AUTH_PASSWORD` is set, and off (loopback
use only) when it is not.

> **There is no exemption for loopback**, deliberately: tunnel daemons connect
> to `127.0.0.1`, so every remote request looks local, and "trust local
> requests" would be a public bypass. Whoever is sitting at the machine logs in
> too.

Scripted access differs between the two. Under ngrok's OAuth the API is
browser-only — `curl` against `/api/*` gets a redirect to Google. The password
gate instead accepts `Authorization: Bearer $FLEET_API_TOKEN` if you set one:

```console
$ curl -H "Authorization: Bearer $FLEET_API_TOKEN" http://127.0.0.1:7879/api/windows
```

### Several machines, one page

Two laptops, two Claude accounts, one dashboard. Each machine runs its own board
over its own `~/.claude` and its own tmux; the machine with the tunnel
aggregates, so the URL you already use shows every machine's cards in one grid,
each tagged with its machine's label, and every button on a card still acts on
the machine that card lives on. (The label is `CLAUDE_FLEET_LABEL`, or the last
octet of that machine's IP if you don't set one.)

```
                    ┌── board on A ────┐  reads ~/.claude on A, drives A's tmux
  public URL ──────▶│  aggregates      │
                    └────────┬─────────┘
                             │ ssh -L 7880:127.0.0.1:7879
                    ┌────────▼─────────┐
                    │  board on B      │  reads ~/.claude on B, drives B's tmux
                    └──────────────────┘
```

**Why not just read the other machine's `~/.claude` over the shared mount?**
Because a card is alive when `/proc/<pid>` exists *on this host* — a remote pid
is either missing or, worse, some unrelated local process — the UI keys cards by
pid, so two hosts collide, and every action is a tmux call on the local machine.
The sessions would render and none of them would be real. So each host keeps its
own board, and cards are addressed by a host-qualified key (`b:1234`) that the
routes use to decide *run it here* or *forward it there*.

On the aggregating host, in `.env.local.<hostname>` (a per-host file `run.sh`
layers on top of the shared `.env.local`):

```bash
FLEET_PEERS=b=http://127.0.0.1:7880
FLEET_PEER_TUNNELS="7880:hostb:7879"   # <local port>:<ssh host>:<peer's port>
```

```console
$ scripts/peer-tunnel.sh start     # also: stop | status
[peer-tunnel] 7880:hostb:7879 — up
$ ./run.sh                         # restart so the board reads FLEET_PEERS
```

The peer is reached on loopback, not on its LAN address: every board binds
`127.0.0.1` because it can type into tmux panes, and an ssh forward gives the
aggregator a local port to poll while the peer stays exactly as unreachable as
before. The password gate applies to that hop too — `FLEET_API_TOKEN` from the
shared `.env.local` is what gets through it.

Peer cards come from a background poll (2s), never from a fetch in the request
path, so a wedged peer costs its own cards going stale — they dim after 10s and
disappear after two minutes — and nothing else. The header grows one chip per
machine: click to see just that machine, and a peer whose board stopped
answering turns red and says why on hover. **Spawn** grows a machine picker, and
the directory list follows it. Search / History / Skills / Memory still show the
aggregating host only.

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
  peers.py            multi-host: poll peer boards, forward card actions
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

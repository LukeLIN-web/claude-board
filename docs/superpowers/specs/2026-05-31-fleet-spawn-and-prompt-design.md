# Claude Fleet — Spawn & Prompt-Injection Design

**Date:** 2026-05-31
**Status:** Approved design, pending implementation plan

## Goal

Extend Claude Fleet from a read-only monitor into one that can also, on explicit
user action:

1. **Create a new Claude Code session fast** — spawn `claude` in a chosen
   directory directly from the dashboard.
2. **Be interactive (send a prompt)** — inject a single text prompt into a
   running session from its card.

Both are deliberate, user-triggered mutations. They join the existing
mutating actions (`fork`, `close`, `review`), so the project's read-only stance
is "read-only *by default*", not absolute.

## Environment (fixed)

- **Linux inside tmux 3.2a** is the detected and target environment.
- `claude` binary resolved on `PATH` (`~/.local/bin/claude`).
- Backend mechanism for **both** features is **tmux**:
  - spawn → `tmux new-window`
  - inject → `tmux send-keys`
- macOS/iTerm2 is explicitly **out of scope** for these two features (the
  existing osascript actions are untouched).

## Decisions (from brainstorming)

| Question | Decision |
|---|---|
| Interaction model | **Send a single prompt** — no embedded terminal, no live TTY streaming |
| Platform/backend | **tmux** (detected from the running environment) |
| New-session cwd | **Recent-dirs dropdown** (built client-side) + free-text override |
| Structure | **Approach A** — dedicated `core/tmux.py` module |

## Architecture

New isolated backend unit `core/tmux.py` owns all tmux interaction. `actions.py`
gets two thin wrappers; `app.py` gets two routes; `index.html` gets the UI. This
matches the existing one-module-one-purpose layout.

```
core/tmux.py     (new)  all tmux subprocess interaction
core/actions.py  (edit) create_session(), send_prompt()
app.py           (edit) POST /api/session/create, POST /api/session/{pid}/prompt,
                        + tmux_available flag in state/SSE payload
static/index.html(edit) New-session popover + per-card prompt input
README.md        (edit) soften the "never mutates" line
```

### 1. `core/tmux.py`

All subprocess calls go through one `_run(*args)` helper:
`subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=10)`.

| Function | Implementation | Returns |
|---|---|---|
| `available()` | `TMUX` env set, or `tmux list-sessions` exits 0 | `bool` |
| `list_panes()` | `tmux list-panes -a -F '#{pane_id}\t#{pane_tty}\t#{session_name}\t#{pane_current_path}'`, parsed | `list[dict]` with keys `pane_id, tty, session, path` |
| `pane_for_tty(tty)` | normalize both sides (strip leading `/dev/`), match against `list_panes()` | `pane_id` or `None` |
| `new_window(cwd)` | `tmux new-window -P -F '#{pane_id}' -t <target> -c <cwd> claude` | `{ok, pane_id, error?}` |
| `send_text(pane, text)` | `tmux send-keys -t <pane> -l -- <text>`, then `tmux send-keys -t <pane> Enter` | `{ok, error?}` |

- **Target session** for `new_window`: `$FLEET_TMUX_SESSION` if set, else the
  first session reported by `tmux list-sessions`.
- `send-keys -l` sends the payload **literally** (never interpreted as key
  names); the separate `Enter` submits it.

### 2. `core/actions.py`

- `create_session(cwd: str) -> dict`
  - Validate `cwd` is a non-empty existing directory → else `{ok: False, error}`.
  - Return `tmux.new_window(cwd)`.
- `send_prompt(pid: int, text: str) -> dict`
  - `find_window(pid)`; resolve its `tty`.
  - `pane = tmux.pane_for_tty(tty)`; if `None` → `{ok: False, error: "session not in a tmux pane"}`.
  - Collapse internal newlines in `text` to spaces (v1 = single line).
  - Return `tmux.send_text(pane, text)`.

### 3. `app.py`

- `POST /api/session/create` body `{cwd}` → `create_session`.
- `POST /api/session/{pid}/prompt` body `{text}` → `send_prompt`.
- Add `tmux_available: bool` (from `tmux.available()`) to the existing
  state/SSE payload so the UI can gate the new controls.
- Match the existing action-route conventions in `app.py`.

### 4. `static/index.html`

- **Header `+ New session`** → inline popover:
  - `<select>` of unique `cwd`s derived client-side from the sessions already in
    state, plus a free-text override field, plus **Spawn**.
  - On success: toast; the new window surfaces on the next 2s poll.
- **Per card:** compact `Send a prompt…` input + **Send** (Enter submits);
  inline ok/error feedback.
- Both controls hidden when `tmux_available` is false.

## Edge cases & safety

- Server-side cwd validation (exists + is a directory).
- `pane_for_tty` miss returns an explicit error — never a silent no-op.
- **v1 single-line prompts**; multiline is a noted future extension.
- No confirmation modal — "fast" was an explicit requirement; Enter sends.
- tmux absent → endpoints return `{ok: False, ...}` and the UI hides the controls.

## Testing

Pure-logic unit tests, no live tmux required (monkeypatch `subprocess.run`):

- `pane_for_tty` tty normalization (`/dev/pts/3` vs `pts/3`, miss → `None`).
- `new_window` and `send_text` build the expected argv (incl. `-l --` and the
  separate `Enter`).
- `create_session` rejects a non-existent / non-directory cwd before touching tmux.

## Out of scope

- Embedded/live terminal (xterm.js + PTY).
- macOS/iTerm2 backend for these two features.
- Multiline prompt injection.
- Quick-action buttons (approve / continue) — could reuse `send_prompt` later.

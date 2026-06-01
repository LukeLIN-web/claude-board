# Claude Fleet — tmux Spawn & Single-Prompt Injection

## Goal Description

Extend Claude Fleet from a read-only monitor into a dashboard that can also perform two explicit, user-triggered mutating actions on Linux under tmux:

1. **Spawn a new Claude Code session fast** — create a new tmux window running `claude` in a chosen working directory, launched directly from the dashboard.
2. **Inject a single prompt** — send one line of text into a running session's tmux pane from that session's card.

Both features are backed entirely by tmux (`tmux new-window` for spawn, `tmux send-keys` for inject). They join the existing mutating actions (`fork`, `close`, `review`), so Fleet's stance becomes "read-only **by default**", not absolute. macOS/iTerm2 is explicitly out of scope for these two features; the existing osascript-based actions are untouched.

All tmux interaction is isolated in a new `core/tmux.py` module (Approach A — one module, one purpose), matching the existing layout. `core/actions.py` gains two thin wrappers, `app.py` gains two routes plus a capability flag, and `static/index.html` gains the UI.

### Resolved scope decisions (from user review of this plan)

- **API route naming**: use the existing `/api/windows/...` convention — `POST /api/windows/create` and `POST /api/windows/{pid}/prompt` — rather than the draft's literal `/api/session/*` paths. (The draft's intent is preserved; only the URL prefix is aligned to the codebase.)
- **Prompt length**: v1 enforces a generous server-side cap of **8000 characters** on the (newline-collapsed) prompt; longer input is rejected with a clear error.
- **Trust model**: v1 assumes the same trusted-local-operator / localhost binding already assumed by the existing `close`/`fork`/`review` actions. No new authentication or bind-address gate is added.

## Acceptance Criteria

Following TDD philosophy, each criterion includes positive and negative tests for deterministic verification. Tests are pure-logic: the tmux CLI is never actually invoked; `subprocess.run` is replaced with a fake so argv construction and error handling are asserted directly.

- AC-1: A new `core/tmux.py` module isolates all tmux subprocess interaction behind a single `_run(*args)` helper, and no tmux function ever raises out to its caller.
  - Positive Tests (expected to PASS):
    - Every tmux subprocess call in the codebase routes through `core.tmux._run`; `_run` returns a structured result dict with keys `ok`, `rc`, `stdout`, `stderr`, `error`.
    - When the fake `subprocess.run` raises `FileNotFoundError` (tmux binary absent), `_run` returns `{ok: False, ...}` with a non-empty `error` and does not propagate the exception.
    - When the fake raises `subprocess.TimeoutExpired`, `_run` returns `{ok: False, ...}` with a timeout error string and does not propagate.
  - Negative Tests (expected to FAIL):
    - A tmux call made with a direct `subprocess.run(["tmux", ...])` outside `_run` is rejected in review (no bypass of the single helper).
    - `_run` re-raising any exception to the caller is a failure.

- AC-2: `tmux.available()` reports whether tmux is usable, and the dashboard's per-poll snapshot does not spawn a fresh tmux subprocess on every cycle.
  - AC-2.1: Detection correctness.
    - Positive: `available()` returns `True` when the `TMUX` environment variable is set; returns `True` when `tmux list-sessions` exits 0; returns `False` when the fake reports tmux missing or `list-sessions` exits non-zero.
    - Negative: `available()` raising when tmux is absent (instead of returning `False`) is a failure.
  - AC-2.2: Availability is cached so the 2-second watcher poll reuses a recent result rather than calling tmux every tick.
    - Positive: With a stubbed clock/counter, building the snapshot N times within the cache window results in at most one underlying availability probe.
    - Negative: Each snapshot build triggering a new `list-sessions` subprocess is a failure.

- AC-3: `list_panes()` and `pane_for_tty(tty)` resolve a tmux pane from a session's TTY, with robust normalization.
  - Positive Tests (expected to PASS):
    - `list_panes()` parses `tmux list-panes -a -F '#{pane_id}\t#{pane_tty}\t#{session_name}\t#{pane_current_path}'` output into `list[dict]` with keys `pane_id, tty, session, path`.
    - `pane_for_tty("/dev/pts/3")` and `pane_for_tty("pts/3")` both match a pane whose reported tty is `/dev/pts/3` (leading `/dev/` and surrounding whitespace normalized on both sides).
  - Negative Tests (expected to FAIL):
    - `pane_for_tty` returns `None` when no pane matches, and `None` for an empty/whitespace tty — never a wrong-pane match and never a raised exception.
    - When the underlying tmux call errors, `list_panes()` returns `[]` rather than raising.

- AC-4: `new_window(cwd)` spawns `claude` in a resolved target session and returns the new pane id.
  - Positive Tests (expected to PASS):
    - Argv equals `tmux new-window -P -F '#{pane_id}' -t <target> -c <cwd> claude`, where `<target>` is `$FLEET_TMUX_SESSION` when set, otherwise the first name from `tmux list-sessions -F '#{session_name}'`.
    - On success the function returns `{ok: True, pane_id: <captured stdout pane id>}`.
  - Negative Tests (expected to FAIL):
    - When no target session can be resolved (no `$FLEET_TMUX_SESSION` and no sessions exist), returns `{ok: False, error: ...}` without attempting `new-window`.
    - When `$FLEET_TMUX_SESSION` is set but does not exist among current sessions, returns a clear `{ok: False, error: ...}` rather than an opaque tmux failure.

- AC-5: `send_text(pane, text)` injects literal text followed by a separate submit keystroke.
  - Positive Tests (expected to PASS):
    - Two ordered tmux calls are issued: `tmux send-keys -t <pane> -l -- <text>` then `tmux send-keys -t <pane> Enter`.
    - The `-l --` form is used so the payload is sent literally and never interpreted as tmux key names.
  - Negative Tests (expected to FAIL):
    - Sending the text and Enter in a single combined call, or omitting `-l --`, is a failure.
    - A non-zero exit on the literal-text call still issuing the Enter call is a failure (errors short-circuit with `{ok: False, error: ...}`).

- AC-6: `actions.create_session(cwd)` validates the working directory before any tmux interaction.
  - Positive Tests (expected to PASS):
    - For an existing directory, returns the result of `tmux.new_window(cwd)`.
    - A leading `~` in `cwd` is expanded server-side before validation (or, if not expanded, rejected with a clear error — never passed raw to tmux).
  - Negative Tests (expected to FAIL):
    - An empty, non-existent, or non-directory `cwd` returns `{ok: False, error: ...}` and the fake `subprocess.run` is never called.

- AC-7: `actions.send_prompt(pid, text)` resolves the session's pane and injects a single sanitized line.
  - Positive Tests (expected to PASS):
    - Resolves `find_window(pid)` → its tty → `tmux.pane_for_tty(tty)`, collapses internal newlines in `text` to single spaces, and returns `tmux.send_text(pane, collapsed)`.
    - A multi-line input is collapsed to one line before sending.
  - Negative Tests (expected to FAIL):
    - When the pid has no resolvable pane, returns `{ok: False, error: "session not in a tmux pane"}` (an explicit error, never a silent no-op).
    - Empty text (or text that is empty after newline collapse/trim) is rejected with a clear error before any send.
    - Text longer than 8000 characters (after collapse) is rejected with a clear length error before any send.

- AC-8: `app.py` exposes the two routes with JSON-body validation and publishes `tmux_available` in the dashboard payload.
  - AC-8.1: Routes.
    - Positive: `POST /api/windows/create` with body `{"cwd": "..."}` dispatches to `actions.create_session`; `POST /api/windows/{pid}/prompt` with body `{"text": "..."}` dispatches to `actions.send_prompt`. Both use one consistent FastAPI body-parsing approach (Pydantic request models).
    - Negative: A request with a missing/empty required field or malformed JSON returns a structured error (FastAPI validation error or `{ok: False, error}`), not an unhandled 500.
  - AC-8.2: Capability flag.
    - Positive: `tmux_available` appears in the payload returned by `GET /api/windows` and in the initial SSE snapshot, including when there are zero windows.
    - Negative: The flag being absent from the zero-windows payload, or its computation adding a tmux subprocess to every 2s diff cycle, is a failure.

- AC-9: `static/index.html` gains both controls, gated on `tmux_available`, and they do not interfere with existing card behavior.
  - Positive Tests (expected to PASS):
    - A header "+ New session" popover offers a `<select>` of unique `cwd`s derived client-side from sessions in state, plus a free-text override field, plus a Spawn action; on success it shows a toast and the new window appears on the next 2s poll.
    - Each session card shows a compact "Send a prompt…" input with a Send action (Enter submits) and inline ok/error feedback.
    - Both controls are hidden when `tmux_available` is `false`.
  - Negative Tests (expected to FAIL):
    - Typing/clicking inside the per-card prompt input toggles the card's expand/collapse (controls must stop event propagation, e.g. `@click.stop`).
    - The Spawn or Send action remaining clickable while a request is in flight (must be disabled to prevent double-spawn / duplicate send).

- AC-10: Documentation read-only claims are reconciled with the new user-triggered actions.
  - Positive Tests (expected to PASS):
    - `README.md`, `README.zh-CN.md`, and `CONTRIBUTING.md` are updated so absolute "never mutates" language reflects "read-only by default, with explicit user-triggered tmux actions when enabled".
    - The reconciliation preserves the accurate claim that Fleet does not write to the stored harness data under `~/.claude/` / `~/.codex/`; the new actions affect live tmux sessions, not those files.
  - Negative Tests (expected to FAIL):
    - Any shipped doc still asserting Fleet "never mutates any agent state" without qualification is a failure.

- AC-11: A pure-logic test suite covers the happy paths and the failure paths and runs without a live tmux.
  - Positive Tests (expected to PASS):
    - Tests run via `python -m unittest` using stdlib `unittest.mock` to replace `subprocess.run`; they cover tty normalization, `new_window`/`send_text` argv (including `-l --` and the separate `Enter`), and `create_session` rejecting a bad cwd before touching tmux.
    - Failure-path tests cover: tmux missing (`FileNotFoundError`), no sessions resolvable, pane-not-found, timeout, and a non-zero tmux exit.
  - Negative Tests (expected to FAIL):
    - A test suite that requires a real tmux server, or that only asserts happy-path argv with no failure-path coverage, is a failure.

## Path Boundaries

Path boundaries define the acceptable range of implementation quality and choices.

### Upper Bound (Maximum Acceptable Scope)
A clean `core/tmux.py` owning all tmux subprocess calls through one never-raising `_run`; cached `available()`; robust `list_panes`/`pane_for_tty` normalization; `new_window` with validated target-session resolution; literal `send_text`; `create_session`/`send_prompt` wrappers honoring the existing structured-dict contract; two convention-named FastAPI routes with Pydantic body validation; a cached `tmux_available` flag in the snapshot; the New-session popover and per-card prompt input gated on availability with in-flight disabling and event-propagation guards; reconciled read-only language across `README.md`, `README.zh-CN.md`, and `CONTRIBUTING.md`; and a stdlib `unittest`-based test suite covering both happy and failure paths.

### Lower Bound (Minimum Acceptable Scope)
`core/tmux.py` provides `available`, `list_panes`/`pane_for_tty`, `new_window`, and `send_text` via a single non-raising `_run`; `actions.create_session` (with cwd validation) and `actions.send_prompt` (pid→tty→pane, newline collapse, empty/length rejection) work end to end; `POST /api/windows/create` and `POST /api/windows/{pid}/prompt` dispatch correctly with body validation; `tmux_available` is present in the snapshot without per-poll subprocess churn; the UI offers spawn + per-card prompt gated on `tmux_available`; at least `README.md` is reconciled; and pure-logic tests cover tty normalization, argv construction (incl. `-l --` + separate Enter), and bad-cwd rejection.

### Allowed Choices
- Can use: tmux as the sole backend; `subprocess.run` for process interaction; Pydantic request models for JSON bodies; stdlib `unittest`/`unittest.mock` for tests; a module-level TTL cache (or app-state cache) for `tmux_available`; Alpine.js patterns already present in `index.html`; existing toast/fetch helpers.
- Cannot use: macOS/iTerm2/osascript paths for these two features; embedded terminal / PTY streaming (xterm.js); multiline prompt injection in v1; a confirmation modal before spawn/send ("fast" was an explicit requirement; Enter sends); any new authentication layer (out of scope per the resolved trust-model decision); adding heavy new runtime dependencies.

> **Note on Deterministic Designs**: The draft fixes most choices (tmux backend, exact argv shapes, module layout, single-line v1, no modal). Those are treated as fixed constraints here; the bounds above converge tightly around the draft. The few genuinely open points were resolved by user review and recorded as fixed decisions in the Goal section.

## Feasibility Hints and Suggestions

> **Note**: This section is for reference and understanding only. These are conceptual suggestions, not prescriptive requirements.

### Conceptual Approach

```
core/tmux.py
  _run(*args):
      try: cp = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=10)
      except FileNotFoundError / TimeoutExpired / OSError: return {ok:False, rc:None, stdout:"", stderr:"", error:<msg>}
      return {ok: cp.returncode == 0, rc: cp.returncode, stdout, stderr, error: stderr if rc else ""}

  available():        # cached: probe at most once per short window
      return bool(os.environ.get("TMUX")) or _run("list-sessions")["ok"]

  list_panes():       # _run("list-panes","-a","-F", "<tab-format>"); parse lines; [] on error
  pane_for_tty(tty):  # norm = lambda s: s.strip().removeprefix("/dev/"); match norm(tty) against panes
  new_window(cwd):    # resolve target (env or first of list-sessions -F '#{session_name}'); validate;
                      # _run("new-window","-P","-F","#{pane_id}","-t",target,"-c",cwd,"claude")
  send_text(pane,t):  # r1=_run("send-keys","-t",pane,"-l","--",t); if not r1.ok return r1
                      # return _run("send-keys","-t",pane,"Enter")

core/actions.py
  create_session(cwd): expand ~, validate existing dir, else {ok:False,error}; return tmux.new_window(cwd)
  send_prompt(pid,text): w=find_window(pid); pane=tmux.pane_for_tty(w.tty); if None -> error;
                         t=" ".join(text.split("\n")); reject empty / len>8000; return tmux.send_text(pane,t)

app.py
  class CreateBody(BaseModel): cwd: str
  class PromptBody(BaseModel): text: str
  @app.post("/api/windows/create")            -> actions.create_session(body.cwd)
  @app.post("/api/windows/{pid}/prompt")      -> actions.send_prompt(pid, body.text)
  _enriched_snapshot(): snap["tmux_available"] = tmux.available()   # cached probe
```

### Relevant References
- `core/tmux.py` (new) — all tmux subprocess interaction.
- `core/actions.py` — existing structured-dict / never-raise action contract to mirror (see `focus_terminal`, `close_session`). Note: `close_session` is currently defined twice in this file; the new wrappers should avoid adding further duplication.
- `core/sessions.py` — `find_window(pid)`, `get_tty(pid)`, the `Window` dataclass (`.tty`, `.cwd`), and `snapshot()`.
- `app.py` — `_enriched_snapshot()` (where `tmux_available` is injected), the existing `@app.post("/api/windows/{pid}/...")` routes, `diff_signature()`, and the SSE `/api/events` generator that emits the first snapshot.
- `static/index.html` — Alpine `x-data="fleet()"`, `this.toast()`, `fetch().then` action methods (`forkWindow`, `closeWindow`, etc.), the card expand handler to guard against, and the toast template.
- `README.md` (`it never mutates any agent state`), `README.zh-CN.md`, `CONTRIBUTING.md` (`never mutates agent state` / read-only principle) — docs to reconcile.
- `pyproject.toml` — currently declares no test dependency; stdlib `unittest` keeps it that way.

## Dependencies and Sequence

### Milestones
1. **Backend tmux core**: Implement `core/tmux.py`.
   - Phase A: `_run` with the structured non-raising result schema.
   - Phase B: `available` (cached), `list_panes`, `pane_for_tty`.
   - Phase C: `new_window` (target resolution + validation), `send_text`.
2. **Action wrappers**: Add `create_session` and `send_prompt` to `core/actions.py`, honoring the existing contract. Depends on Milestone 1.
3. **API surface**: Add the two routes and the cached `tmux_available` flag to `app.py`. Depends on Milestone 2.
4. **Frontend controls**: Add the New-session popover and per-card prompt input to `static/index.html`, gated on `tmux_available`, with in-flight disabling and `@click.stop`. Depends on Milestone 3 (consumes the flag and the routes).
5. **Docs reconciliation**: Update `README.md`, `README.zh-CN.md`, `CONTRIBUTING.md`. Independent of the code milestones; can proceed in parallel.
6. **Tests**: Pure-logic `unittest` suite for the tmux core and action wrappers (happy + failure paths). Depends on Milestones 1–2; expands as 3 lands.

Dependency summary: tmux core → action wrappers → routes → UI. Docs are independent. Tests follow the core/wrappers and grow with the routes.

## Task Breakdown

Each task must include exactly one routing tag:
- `coding`: implemented by Claude
- `analyze`: executed via Codex (`/humanize:ask-codex`)

| Task ID | Description | Target AC | Tag (`coding`/`analyze`) | Depends On |
|---------|-------------|-----------|----------------------------|------------|
| task1 | Create `core/tmux.py` with `_run` (structured, never-raising result schema `{ok,rc,stdout,stderr,error}`) | AC-1 | coding | - |
| task2 | Implement cached `available()` (TMUX env or `list-sessions` exit 0) | AC-2 | coding | task1 |
| task3 | Implement `list_panes()` + `pane_for_tty()` with two-sided tty normalization | AC-3 | coding | task1 |
| task4 | Implement `new_window(cwd)` with target resolution (`$FLEET_TMUX_SESSION` / first `list-sessions -F '#{session_name}'`) and validation | AC-4 | coding | task1 |
| task5 | Implement `send_text(pane,text)` as literal `-l --` send then separate `Enter` | AC-5 | coding | task1 |
| task6 | Add `actions.create_session(cwd)` with `~` expansion + directory validation before tmux | AC-6 | coding | task1,task4 |
| task7 | Add `actions.send_prompt(pid,text)` (pid→tty→pane, newline collapse, empty + 8000-char rejection) | AC-7 | coding | task3,task5 |
| task8 | Add `POST /api/windows/create` and `POST /api/windows/{pid}/prompt` with Pydantic bodies | AC-8 | coding | task6,task7 |
| task9 | Inject cached `tmux_available` into `_enriched_snapshot()` payload (present even with zero windows; no per-poll subprocess) | AC-8 | coding | task2 |
| task10 | Add New-session popover + per-card prompt input in `index.html`, gated on `tmux_available`, with `@click.stop` and in-flight disabling | AC-9 | coding | task8,task9 |
| task11 | Reconcile read-only claims in `README.md`, `README.zh-CN.md`, `CONTRIBUTING.md` | AC-10 | coding | - |
| task12 | Write `unittest`/`unittest.mock` suite covering happy + failure paths, runnable via `python -m unittest` | AC-1,AC-3,AC-4,AC-5,AC-6,AC-7,AC-11 | coding | task1,task2,task3,task4,task5,task6,task7 |
| task13 | Independent reasonability re-check of the final tmux argv and failure-path coverage | AC-5,AC-11 | analyze | task5,task12 |

## Claude-Codex Deliberation

### Agreements
- Isolating all tmux interaction in a new `core/tmux.py` behind a single `_run`, with thin `actions.py` wrappers, matches the repo's one-module-one-purpose layout.
- The structured-result / never-raise contract, two-sided tty normalization, `send-keys -l -- <text>` plus a separate `Enter`, server-side cwd validation before tmux, and failure-path tests are the correct shapes.
- Linux/tmux-only scope; no macOS backend expansion; existing osascript actions untouched.
- `tmux_available` must be cached so the 2-second watcher poll does not spawn a tmux subprocess every tick, and must be present in the snapshot even with zero windows.
- `_run` must catch `FileNotFoundError`, `TimeoutExpired`, and `OSError` and return structured data with an explicit schema (`{ok, rc, stdout, stderr, error}`).
- Target-session fallback should parse `tmux list-sessions -F '#{session_name}'` (not tmux's default colon-formatted output).

### Resolved Disagreements
- **API route naming** (draft `/api/session/*` vs convention `/api/windows/*`): Claude and Codex both recommended the existing `/api/windows/...` convention; the user confirmed. Resolution: `POST /api/windows/create` and `POST /api/windows/{pid}/prompt`. Rationale: consistency with all existing action routes; the draft's feature intent is fully preserved, only the URL prefix changes.
- **Docs scope** (draft mentions only `README.md`): Codex noted `README.zh-CN.md` and `CONTRIBUTING.md` also carry absolute "never mutates" language. Resolution: reconcile all three, while preserving the accurate claim that Fleet does not write to stored harness data under `~/.claude/`/`~/.codex/`.
- **Frontend event bubbling**: cards already have an expand click handler. Resolution: per-card prompt controls must `@click.stop` and handle Enter without toggling the card.
- **Test runner** (repo declares no pytest): Resolution: use stdlib `unittest`/`unittest.mock` run via `python -m unittest`, adding no new dependency. The draft's "monkeypatch `subprocess.run`" intent is satisfied by `unittest.mock.patch`.
- **`tmux_available` caching semantics**: Resolution: a single cached probe (TTL/app-state), explicitly not both startup-static and TTL at once.
- **Prompt length** (draft silent): Resolution: generous 8000-char server-side cap with a clear rejection.
- **Trust model**: Resolution: same trusted-local assumption as existing `close`/`fork`/`review`; no new auth in v1.

### Convergence Status
- Final Status: `converged`
- Rounds executed: Codex first-pass analysis (1) + one convergence review round; all `REQUIRED_CHANGES` were accepted and folded in, and all open questions were resolved by user review.

## Pending User Decisions

_None. All decisions surfaced during planning were resolved by user review and are recorded as fixed decisions in the Goal section and Resolved Disagreements above._

- DEC-1: API route naming — **Resolved**: use `/api/windows/create` and `/api/windows/{pid}/prompt` (convention-aligned).
- DEC-2: Maximum v1 prompt length — **Resolved**: generous 8000-character cap with a clear rejection error.
- DEC-3: Trust model for the new mutating routes — **Resolved**: localhost/trusted-only, matching existing actions; no new auth.

## Implementation Notes

### Code Style Requirements
- Implementation code and comments must NOT contain plan-specific terminology such as "AC-", "Milestone", "Step", "Phase", or similar workflow markers.
- These terms are for plan documentation only, not for the resulting codebase.
- Use descriptive, domain-appropriate naming in code instead (e.g., `tmux_available`, `pane_for_tty`, `create_session`).
- New action functions must follow the existing contract in `core/actions.py`: return a structured dict and never raise out to the FastAPI route handler.
- Avoid adding a third duplicate of `close_session`; the existing file already defines it twice.

### Verification
- Run the test suite with `python -m unittest` from the repo root; it must pass without a live tmux server.
- Manually confirm `GET /api/windows` includes `tmux_available` even when no sessions are present.

--- Original Design Draft Start ---

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

--- Original Design Draft End ---

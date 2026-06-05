# Design: Show queued prompts on each session card

**Date:** 2026-06-04
**Status:** Approved

## Problem

When a Claude session is busy and the user sends several prompts in a row
(from the claude-fleet dashboard, or by typing directly in the Claude TUI),
Claude Code queues them internally. The dashboard cards show no record of this
backlog, so the user cannot see what is still waiting to be processed.

Goal: each session card displays the prompts currently queued for that session.

## Key finding (drives the design)

Claude's TUI *does* render queued messages in the pane, but the format is
inconsistent:

- **Multiple queued (steady state):** each item renders as an indented `❯ <text>`
  line stacked just above the input box, and the input box placeholder becomes
  `Press up to edit queued messages` — a reliable "a queue exists" signal.
- **Single queued / mid-transition:** renders as `❯ <text>` (no indent) up near
  the spinner, placeholder may still be empty. Less predictable.
- Queued lines visually resemble the active prompt line and normal `❯` output,
  so distinguishing "queued" from "being processed" by scraping alone is
  imperfect.

Consequence: dashboard-sent prompts can be tracked 100% reliably (claude-fleet
is the sender). TUI-typed queued prompts can only be scraped best-effort.

## Chosen approach: Hybrid

Track dashboard-sent prompts reliably server-side; additionally scrape the pane
to surface TUI-typed queued items best-effort. The card shows a combined
"Queued (N)" list that distinguishes the two sources.

Rejected alternatives:
- **Scrape-only:** simplest data model, but inherits the full scrape fragility
  for *all* items, including ones we could track perfectly.
- **Dashboard-only:** 100% reliable but ignores TUI-typed prompts, which the
  user explicitly wants to see.

## Components

### 1. `core/promptqueue.py` (new) — reliable dashboard-sent tracking

Module-level store: `{pid: [{id, text_norm, ts}]}`.

- `record_sent(pid, text)` — append an item. `text_norm` is whitespace-collapsed
  the same way `actions.send_prompt` already collapses newlines, so it matches
  what later appears in the transcript.
- `pending(pid, transcript_path, status)` — return tracked items Claude has not
  processed yet:
  - **Consumed:** an item is dropped once a matching `user_text` transcript event
    (same `text_norm`, event timestamp ≥ item `ts`) appears. Duplicate texts are
    matched in send order (one transcript hit clears one tracked item).
  - **Idle clear:** when `status == "idle"`, clear the whole pid list — a queue
    cannot survive an idle session.

This module owns all reliable-queue state and reconciliation; it has no
dependency on tmux.

### 2. `core/actions.py` — best-effort TUI scrape (mirrors existing menu parsing)

Follow the existing `parse_pane_menu` / `get_pane_menu` pattern.

- `parse_pane_queue(text)` — given a captured pane:
  1. Locate the input box: the region between the two bottom `────` horizontal
     rules. The line(s) inside are the current input draft — **ignore them**.
  2. Detect a queue via the `Press up to edit queued messages` placeholder
     and/or a contiguous block of indented `❯ ` lines immediately above the box.
  3. Return that block's lines as queued item texts.
  - Best-effort: reliable for the multi-item case; may miss the
    single-item / transitioning case (accepted per the tradeoff above).
- `get_pane_queue(pid)` — two captures (visible viewport + scrollback) like
  `get_pane_menu`, to avoid reporting scrollback ghosts.

### 3. `app.py` `_enriched_snapshot()` — combine per window

Only for windows with `status == "busy"` (a queue only exists while busy; this
also bounds the extra `tmux capture-pane` cost to busy sessions). Build
`w["queued"]`:

1. Start with `promptqueue.pending(pid, tp, status)` → each tagged
   `source: "dashboard"` (reliable).
2. Add scraped items (`actions.get_pane_queue(pid)`) whose `text_norm` does not
   match any dashboard item → tagged `source: "tui"` (best-effort).
3. Dedup by `text_norm`. Each entry: `{text, source}`.

Non-busy windows get `queued: []`.

### 4. API + UI

- The `/api/windows/{pid}/prompt` handler in `app.py` calls
  `promptqueue.record_sent(pid, text)` after a successful send.
- `static/index.html`: each card shows a "Queued (N)" section listing the items,
  each with a small source badge (📥 dashboard / ⌨ tui). Hidden when N = 0.

## Data flow

```
send via dashboard ──> POST /prompt ──> actions.send_prompt (tmux inject)
                                   └──> promptqueue.record_sent(pid, text)

watcher (2s) ──> _enriched_snapshot()
                   for busy w:
                     dashboard = promptqueue.pending(pid, tp, status)   [reliable]
                     tui       = actions.get_pane_queue(pid)            [best-effort]
                     w["queued"] = dedup(dashboard + tui)
                 ──> SSE ──> card renders "Queued (N)"
```

## Error handling

- Scrape failure (no pane / tmux error / parse miss) degrades that window to
  dashboard-only; it must never raise out of `_enriched_snapshot()`.
- `promptqueue` is in-memory only; a server restart loses tracking (acceptable —
  the idle-clear and transcript reconciliation re-converge as sessions drain).

## Testing

- `parse_pane_queue` unit tests against the real captured fixtures: the 3-item
  case, the 1-item case, and the no-queue case.
- `promptqueue` unit tests: consumed-by-transcript removal, cleared-on-idle,
  duplicate-text ordering.
- Combine/dedup: a dashboard-sent prompt still queued must appear once
  (tagged dashboard), not duplicated by the scrape.

## Out of scope

- Reordering / editing / deleting queued items from the dashboard.
- Persisting the tracking store across server restarts.

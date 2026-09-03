#!/bin/bash
# Claude Fleet launcher. First run will create .venv and install deps.
set -e
cd "$(dirname "$0")"

# Per-host overrides (gitignored). Use for machine-specific settings like
# CLAUDE_FLEET_CWD_INCLUDE without committing them. Absent on other hosts.
if [ -f .env.local ]; then
    set -a; source .env.local; set +a
fi

# Per-host additions layered on top of the shared file above. This repo is
# served to every host off one mount, so .env.local reaches all of them —
# anything true of exactly one machine (its peer list, say) belongs here.
if [ -f ".env.local.$(hostname)" ]; then
    set -a; source ".env.local.$(hostname)"; set +a
fi

if [ ! -d .venv ]; then
    echo "[claude-fleet] creating venv..."
    python3 -m venv .venv
fi

source .venv/bin/activate

if ! python -c "import fastapi" 2>/dev/null; then
    echo "[claude-fleet] installing deps..."
    pip install -q -e .
fi

PORT="${CLAUDE_FLEET_PORT:-7879}"
echo "[claude-fleet] listening on http://127.0.0.1:${PORT}"

# This repo often lives on a network/shared mount (e.g. /shared) where inotify
# events don't fire, so uvicorn's default --reload silently never detects edits
# and the server keeps serving stale code. Force watchfiles into polling mode and
# scope the watch to this dir so reload actually works here.
export WATCHFILES_FORCE_POLLING=1
RELOAD_ARGS=(--reload --reload-dir .)

# Foreground mode is intentionally unsupervised: it belongs to the calling
# terminal and Ctrl-C should stop it. Detached mode goes through the supervisor,
# which survives the shell and restarts uvicorn whenever it exits.
if [ -n "$CLAUDE_FLEET_FOREGROUND" ]; then
    exec uvicorn app:app --host 127.0.0.1 --port "$PORT" "${RELOAD_ARGS[@]}"
fi

exec scripts/board-supervisor.sh "${1:-start}"

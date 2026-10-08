#!/bin/bash
# Claude Fleet launcher. First run will create .venv and install deps.
set -e
cd "$(dirname "$0")"

# .env.local, then .env.local.<hostname>; sets PORT.
source scripts/env.sh

if [ ! -d .venv ]; then
    echo "[claude-fleet] creating venv..."
    python3 -m venv .venv
fi

source .venv/bin/activate

if ! python -c "import fastapi" 2>/dev/null; then
    echo "[claude-fleet] installing deps..."
    pip install -q -e .
fi

echo "[claude-fleet] listening on http://127.0.0.1:${PORT}"

# Foreground mode is intentionally unsupervised: it belongs to the calling
# terminal and Ctrl-C should stop it. Detached mode goes through the supervisor,
# which survives the shell and restarts uvicorn whenever it exits. Both launch
# the same way (exec_board, scripts/env.sh).
if [ -n "$CLAUDE_FLEET_FOREGROUND" ]; then
    exec_board
fi

exec scripts/board-supervisor.sh "${1:-start}"

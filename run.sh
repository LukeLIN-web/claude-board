#!/bin/bash
# Claude Fleet launcher. First run will create .venv and install deps.
set -e
cd "$(dirname "$0")"

# Per-host overrides (gitignored). Use for machine-specific settings like
# CLAUDE_FLEET_CWD_INCLUDE without committing them. Absent on other hosts.
if [ -f .env.local ]; then
    set -a; source .env.local; set +a
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

# By default run detached so the server survives the launching shell/session.
# Set CLAUDE_FLEET_FOREGROUND=1 to run in the foreground instead.
if [ -n "$CLAUDE_FLEET_FOREGROUND" ]; then
    exec uvicorn app:app --host 127.0.0.1 --port "$PORT" --reload
fi

setsid uvicorn app:app --host 127.0.0.1 --port "$PORT" --reload \
    > uvicorn.log 2>&1 < /dev/null &
echo "[claude-fleet] started detached (pid $!), logs -> uvicorn.log"

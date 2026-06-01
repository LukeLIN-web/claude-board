#!/bin/bash
# Claude Fleet launcher. First run will create .venv and install deps.
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    echo "[claude-fleet] creating venv..."
    python3 -m venv .venv
fi

source .venv/bin/activate

if ! python -c "import fastapi" 2>/dev/null; then
    echo "[claude-fleet] installing deps..."
    pip install -q -e .
fi

PORT="${CLAUDE_FLEET_PORT:-7878}"
echo "[claude-fleet] listening on http://127.0.0.1:${PORT}"
exec uvicorn app:app --host 127.0.0.1 --port "$PORT" --reload

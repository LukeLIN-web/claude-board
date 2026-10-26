#!/usr/bin/env bash
# Regenerate the README screenshots from synthetic demo data.
#
#   1. serve the board on fixtures/demo-home (scripts/demo-board.sh)
#   2. drive headless Chrome to capture each panel via hash deep-links
#   3. tear everything down
#
# No real user data is ever involved. Requires Google Chrome / Chromium.
# Override the browser with CHROME=/path/to/chrome.
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${SHOT_PORT:-7901}"
DEMO="$PWD/fixtures/demo-home"
OUT="$PWD/docs"

# The board's venv, where run.sh keeps it (VENV in scripts/env.sh, which this
# doesn't source: it would load .env.local's port and password into the demo).
# Resolved before HOME moves below.
VENV="${CLAUDE_FLEET_VENV:-${XDG_DATA_HOME:-$HOME/.local/share}/claude-fleet/venv}"
source "$VENV/bin/activate" 2>/dev/null || { mkdir -p "$(dirname "$VENV")" && python3 -m venv "$VENV" && source "$VENV/bin/activate"; }
python -c "import fastapi" 2>/dev/null || pip install -q -e .
PY=python

# Published, so nothing of this host: the demo board's isolation, plus only the
# demo cwd (/proc discovery cards real sessions) and a label that isn't the IP
# octet.
source scripts/demo-board.sh
export CLAUDE_FLEET_CWD_INCLUDE=/home/dev/acme CLAUDE_FLEET_LABEL=laptop
find_chrome
trap stop_demo_board EXIT
start_demo_board

shot() {  # <hash-or-empty> <height> <outfile>
  local url="http://127.0.0.1:$PORT/?snapshot${1:+#$1}"
  rm -rf "/tmp/cprof_$3"
  timeout 30 "$CHROME" --headless --disable-gpu --no-sandbox --hide-scrollbars \
    --no-first-run --no-default-browser-check --disable-background-networking \
    --disable-component-update --disable-default-apps --force-device-scale-factor=2 \
    --user-data-dir="/tmp/cprof_$3" --window-size="1440,$2" --virtual-time-budget=7000 \
    --screenshot="$OUT/$3" "$url" >/dev/null 2>&1 || true
  [ -s "$OUT/$3" ] && echo "  ✓ $3" || { echo "  ✗ $3 (not written)"; return 1; }
}

echo "· capturing → $OUT"
shot ""                                  1600 screenshot-hero.png
shot "skills"                            1520 screenshot-skills.png
shot "memory"                            1520 screenshot-memory.png
shot "search=postgres"                   1200 screenshot-search.png
shot "timeline=demo-0005-migrate-postgres" 1720 screenshot-timeline.png

echo "✓ done"

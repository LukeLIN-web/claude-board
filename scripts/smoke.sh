#!/usr/bin/env bash
# Boot the board against synthetic demo data and check that it serves and renders.
#
#   1. seed a throwaway home with fixtures/seed.py (fake "Acme" sessions)
#   2. start uvicorn on a free port, pointed at that home
#   3. every read-only route the page loads answers 200 with the demo's data
#   4. headless Chrome renders the page: the demo cards are in the DOM and the
#      console has no uncaught error. static/index.html is one hand-edited file
#      of inline Alpine, and a syntax error in it renders an empty board, which
#      nothing else catches.
#
# No real user data is involved (scripts/demo-board.sh).
#
#   bash scripts/smoke.sh                  # uses python3 and the first Chrome found
#   PYTHON=~/.local/share/claude-fleet/venv/bin/python bash scripts/smoke.sh
#   CHROME=/path/to/chrome bash scripts/smoke.sh
#   SMOKE_NO_BROWSER=1 bash scripts/smoke.sh   # steps 1-3 only
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
DEMO="$(mktemp -d "${TMPDIR:-/tmp}/claude-board-smoke.XXXXXX")"
PORT="$("$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
BASE="http://127.0.0.1:$PORT"

source scripts/demo-board.sh
trap 'stop_demo_board; rm -rf "$DEMO"' EXIT
start_demo_board

echo "· checking the API"
BASE="$BASE" "$PY" - <<'EOF'
import json
import os
import sys
import urllib.request

base = os.environ["BASE"]
failures = []


def get(path):
    with urllib.request.urlopen(base + path, timeout=20) as r:
        body = r.read()
        assert r.status == 200, f"{path}: HTTP {r.status}"
    return body


def check(path, test, what):
    try:
        body = get(path)
        data = json.loads(body) if path.startswith("/api/") else body.decode()
        if not test(data):
            raise AssertionError(what)
        print(f"  ✓ {path}")
        return data
    except Exception as e:
        failures.append(f"{path}: {e}")
        print(f"  ✗ {path}: {e}")
        return None


check("/", lambda html: "x-data" in html, "the page has no Alpine root")
win = check("/api/windows",
            lambda d: {"build-auth-api", "migrate-postgres-15"}
            <= {w.get("name") for w in d.get("windows", [])},
            "the seeded live sessions are not on the board")
if win:
    key = next(w["key"] for w in win["windows"] if w.get("name") == "migrate-postgres-15")
    check(f"/api/windows/{key}/timeline",
          lambda d: "Postgres 15" in json.dumps(d), "the timeline lost the seeded turns")
check("/api/history", lambda d: "Cut the 2.4 release" in json.dumps(d),
      "history-only sessions are missing")
check("/api/skills", lambda d: "db-migrate" in json.dumps(d), "seeded skills are missing")
check("/api/memory", lambda d: "db-guidelines" in json.dumps(d), "seeded memories are missing")
check("/api/search?q=postgres", lambda d: "ostgres" in json.dumps(d), "search found nothing")

if failures:
    sys.exit(1)
EOF

if [ -n "${SMOKE_NO_BROWSER:-}" ]; then
  echo "✓ smoke passed (browser step skipped)"
  exit 0
fi

find_chrome

echo "· rendering the page in headless Chrome"
# ?snapshot draws the board once without opening the event stream, so the
# virtual-time budget runs out on a settled page.
timeout 60 "$CHROME" --headless --no-sandbox --disable-gpu --no-first-run \
  --no-default-browser-check --disable-component-update --disable-default-apps \
  --user-data-dir="$DEMO/chrome" --enable-logging=stderr --v=0 \
  --virtual-time-budget=10000 --dump-dom "$BASE/?snapshot" \
  >"$DEMO/dom.html" 2>"$DEMO/chrome.log" || true

status=0
for name in build-auth-api fix-flaky-tests migrate-postgres-15; do
  if grep -q "$name" "$DEMO/dom.html"; then
    echo "  ✓ card $name rendered"
  else
    echo "  ✗ card $name missing from the rendered page" >&2
    status=1
  fi
done
if grep -E "Uncaught|CONSOLE.*(Error|error)" "$DEMO/chrome.log" >&2; then
  echo "  ✗ the page logged an error to the console (above)" >&2
  status=1
fi
if [ "$status" -ne 0 ]; then
  echo "--- chrome log ---" >&2; tail -40 "$DEMO/chrome.log" >&2
  exit 1
fi
echo "✓ smoke passed"

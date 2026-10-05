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
# No real user data is involved: HOME and CLAUDE_FLEET_HOME both point at the
# throwaway home, and tmux calls go to a private socket that has no server.
#
#   bash scripts/smoke.sh                  # uses python3 and the first Chrome found
#   PYTHON=.venv/bin/python bash scripts/smoke.sh
#   CHROME=/path/to/chrome bash scripts/smoke.sh
#   SMOKE_NO_BROWSER=1 bash scripts/smoke.sh   # steps 1-3 only
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
DEMO="$(mktemp -d "${TMPDIR:-/tmp}/claude-board-smoke.XXXXXX")"
PORT="$("$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
BASE="http://127.0.0.1:$PORT"

cleanup() {
  [ -n "${SRV_PID:-}" ] && kill "$SRV_PID" 2>/dev/null || true
  CLAUDE_FLEET_HOME="$DEMO" "$PY" fixtures/seed.py --stop >/dev/null 2>&1 || true
  rm -rf "$DEMO"
}
trap cleanup EXIT

# What a host's .env.local can set that would change what this run sees: a
# password gate, peers to aggregate, cwd filters, a pinned tmux target.
unset FLEET_AUTH_PASSWORD FLEET_API_TOKEN FLEET_PEERS CLAUDE_FLEET_LABEL \
      CLAUDE_FLEET_CWD_INCLUDE CLAUDE_FLEET_CWD_EXCLUDE FLEET_TMUX_SESSION TMUX
export HOME="$DEMO" CLAUDE_FLEET_HOME="$DEMO" FLEET_TMUX_SOCKET="claude-board-smoke-$$"

echo "· seeding demo data into $DEMO"
"$PY" fixtures/seed.py >/dev/null

echo "· starting the board on :$PORT"
"$PY" -m uvicorn app:app --host 127.0.0.1 --port "$PORT" >"$DEMO/uvicorn.log" 2>&1 &
SRV_PID=$!

for _ in $(seq 1 60); do
  curl -sf -o /dev/null "$BASE/api/windows" && break
  kill -0 "$SRV_PID" 2>/dev/null || break
  sleep 0.5
done
if ! curl -sf -o /dev/null "$BASE/api/windows"; then
  echo "✗ the board never answered on :$PORT" >&2
  cat "$DEMO/uvicorn.log" >&2
  exit 1
fi

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


def check(path, test=None, what=""):
    try:
        body = get(path)
        data = json.loads(body) if path.startswith("/api/") else body.decode()
        if test is not None and not test(data):
            raise AssertionError(what or "unexpected payload")
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
check("/api/plans")
check("/api/perms")

if failures:
    sys.exit(1)
EOF

if [ -n "${SMOKE_NO_BROWSER:-}" ]; then
  echo "✓ smoke passed (browser step skipped)"
  exit 0
fi

CHROME="${CHROME:-}"
if [ -z "$CHROME" ]; then
  for c in google-chrome google-chrome-stable chromium chromium-browser chrome-headless-shell \
           "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"; do
    if command -v "$c" >/dev/null 2>&1 || [ -x "$c" ]; then CHROME="$c"; break; fi
  done
fi
[ -n "$CHROME" ] || { echo "✗ no Chrome/Chromium found; set CHROME=... or SMOKE_NO_BROWSER=1" >&2; exit 1; }

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

# scripts/demo-board.sh — sourced, never run. smoke.sh and gen-screenshots.sh
# source it, from the repo root, to serve the board on synthetic demo data with
# nothing of this host in it. The caller sets PY (the python to run it with),
# DEMO (the demo home) and PORT first.
#
# Sourcing it isolates the shell: HOME and CLAUDE_FLEET_HOME both point at
# DEMO, tmux calls go to a private socket that has no server, and what a host's
# environment can set to change what the board shows is unset — a password
# gate, peers to aggregate, cwd filters, a label, a pinned tmux target.
unset FLEET_AUTH_PASSWORD FLEET_API_TOKEN FLEET_PEERS CLAUDE_FLEET_LABEL \
      CLAUDE_FLEET_CWD_INCLUDE CLAUDE_FLEET_CWD_EXCLUDE FLEET_TMUX_SESSION TMUX
export HOME="$DEMO" CLAUDE_FLEET_HOME="$DEMO" FLEET_TMUX_SOCKET="claude-board-demo-$$"

# start_demo_board — seed DEMO with fake "Acme" sessions (fixtures/seed.py),
# start uvicorn on PORT in the background (SRV_PID, log in DEMO/uvicorn.log),
# and return once it answers; exit 1 with the log if it never does.
start_demo_board() {
  echo "· seeding demo data into $DEMO"
  "$PY" fixtures/seed.py >/dev/null
  echo "· starting the board on :$PORT"
  "$PY" -m uvicorn app:app --host 127.0.0.1 --port "$PORT" >"$DEMO/uvicorn.log" 2>&1 &
  SRV_PID=$!
  for _ in $(seq 1 60); do
    curl -sf -o /dev/null "http://127.0.0.1:$PORT/api/windows" && return 0
    kill -0 "$SRV_PID" 2>/dev/null || break
    sleep 0.5
  done
  echo "✗ the board never answered on :$PORT" >&2
  cat "$DEMO/uvicorn.log" >&2
  exit 1
}

# stop_demo_board — the board and the fake session processes, for an EXIT trap.
stop_demo_board() {
  [ -n "${SRV_PID:-}" ] && kill "$SRV_PID" 2>/dev/null || true
  "$PY" fixtures/seed.py --stop >/dev/null 2>&1 || true
}

# find_chrome — set CHROME to the first Chrome/Chromium found, unless the
# caller's environment already names one; exit 1 when there is none.
find_chrome() {
  CHROME="${CHROME:-}"
  if [ -z "$CHROME" ]; then
    for c in google-chrome google-chrome-stable chromium chromium-browser chrome-headless-shell \
             "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
             "/Applications/Chromium.app/Contents/MacOS/Chromium"; do
      if command -v "$c" >/dev/null 2>&1 || [ -x "$c" ]; then CHROME="$c"; break; fi
    done
  fi
  [ -n "$CHROME" ] || { echo "✗ no Chrome/Chromium found; set CHROME=..." >&2; exit 1; }
}

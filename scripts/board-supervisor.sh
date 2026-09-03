#!/usr/bin/env bash
# board-supervisor.sh [start|stop|restart|status] — keep the local board alive.
#
# The supervisor is deliberately a plain detached process instead of a systemd
# unit: this project is also run on machines where the user has no systemd
# session. It owns uvicorn's process group and starts it again whenever it exits.
# A board started by an older run.sh is left alone; the supervisor waits for its
# port to become free and takes over then.
#
# Config (in .env.local and .env.local.<hostname>, as in run.sh):
#   CLAUDE_FLEET_PORT           board port (default 7879)
#   FLEET_BOARD_RESTART_DELAY  seconds between restarts (default 3)
#
# Exit codes: 0 ok · 2 usage/config
set -uo pipefail
cd "$(dirname "$0")/.."

if [ -f .env.local ]; then
    set -a; source .env.local; set +a
fi
if [ -f ".env.local.$(hostname)" ]; then
    set -a; source ".env.local.$(hostname)"; set +a
fi

PORT="${CLAUDE_FLEET_PORT:-7879}"
RESTART_DELAY="${FLEET_BOARD_RESTART_DELAY:-3}"
RUN_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/claude-fleet"
PID_FILE="$RUN_DIR/board-supervisor.pid"
LOCK_FILE="$RUN_DIR/board-supervisor.lock"
LOG_FILE="uvicorn.$(hostname).log"

supervisor_running() {
    [ -s "$PID_FILE" ] || return 1
    pid="$(cat "$PID_FILE" 2>/dev/null)"
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1

    # A stale pid file must never let `stop` signal an unrelated, reused pid.
    # The internal mode in the command line is a cheap but strong identity check.
    [ -r "/proc/$pid/cmdline" ] || return 1
    cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline")"
    case "$cmdline" in
        *board-supervisor.sh*__supervise*) return 0 ;;
        *) return 1 ;;
    esac
}

board_code() {
    curl -s -o /dev/null -w '%{http_code}' --max-time 2 \
        "http://127.0.0.1:$PORT/" 2>/dev/null || true
}

port_listening() {
    [ "$(board_code)" != "000" ]
}

stop_supervisor() {
    if ! supervisor_running; then
        rm -f "$PID_FILE"
        echo "[board-supervisor] not running"
        return 0
    fi

    pid="$(cat "$PID_FILE")"
    # `start` uses setsid, so this reaches both the loop and every uvicorn
    # reloader/worker it owns. TERM lets uvicorn finish in-flight requests.
    kill -TERM -- "-$pid" 2>/dev/null || true
    for _ in $(seq 20); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.25
    done
    if kill -0 "$pid" 2>/dev/null; then
        echo "[board-supervisor] pid $pid did not stop after 5s" >&2
        return 1
    fi
    rm -f "$PID_FILE"
    echo "[board-supervisor] stopped"
}

case "${1:-start}" in
# Internal mode. Not for direct use.
__supervise)
    own_pid="$$"
    cleanup() {
        if [ "$(cat "$PID_FILE" 2>/dev/null)" = "$own_pid" ]; then
            rm -f "$PID_FILE"
        fi
    }
    trap cleanup EXIT

    while true; do
        # This makes installation safe while a pre-supervisor run.sh instance
        # is alive: do not fight it for the port, just be ready when it leaves.
        while port_listening; do sleep 2; done

        echo "[board-supervisor] $(date -Is) starting board on 127.0.0.1:$PORT"
        WATCHFILES_FORCE_POLLING=1 .venv/bin/uvicorn app:app \
            --host 127.0.0.1 --port "$PORT" --reload --reload-dir . &
        child="$!"
        wait "$child"
        code="$?"
        echo "[board-supervisor] $(date -Is) board exited with status $code; restarting in ${RESTART_DELAY}s"
        sleep "$RESTART_DELAY"
    done
    ;;
stop)
    stop_supervisor
    exit $?
    ;;
restart)
    stop_supervisor || exit $?
    ;;
status)
    code="$(board_code)"
    if supervisor_running; then
        pid="$(cat "$PID_FILE")"
        if [ "$code" = "000" ]; then
            echo "[board-supervisor] active (pid $pid); board is between restarts"
        else
            echo "[board-supervisor] active (pid $pid); board HTTP $code on 127.0.0.1:$PORT"
        fi
    elif [ "$code" != "000" ]; then
        echo "[board-supervisor] NOT running; an unmanaged board answers HTTP $code on 127.0.0.1:$PORT" >&2
    else
        echo "[board-supervisor] not running; board is down"
    fi
    exit 0
    ;;
start) ;;
*)
    echo "usage: scripts/board-supervisor.sh [start|stop|restart|status]" >&2
    exit 2
    ;;
esac

# --- start ------------------------------------------------------------------
mkdir -p "$RUN_DIR"
exec 9>"$LOCK_FILE"
flock 9

if supervisor_running; then
    pid="$(cat "$PID_FILE")"
    echo "[board-supervisor] already active (pid $pid)"
    exit 0
fi
rm -f "$PID_FILE"

setsid "$0" __supervise >> "$LOG_FILE" 2>&1 < /dev/null 9>&- &
pid="$!"
printf '%s\n' "$pid" > "$PID_FILE"

# Confirm the detached shell survived exec before claiming protection is on.
sleep 0.2
if ! supervisor_running; then
    rm -f "$PID_FILE"
    echo "[board-supervisor] failed to start; see $LOG_FILE" >&2
    exit 2
fi

if port_listening; then
    echo "[board-supervisor] active (pid $pid); board HTTP $(board_code) on 127.0.0.1:$PORT"
else
    echo "[board-supervisor] active (pid $pid); board is starting, logs -> $LOG_FILE"
fi

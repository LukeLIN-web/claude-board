# scripts/env.sh — sourced, never run. run.sh and every scripts/*.sh that reads
# the board's config source this right after cd-ing to the repo root, so they
# all load the same files in the same order and agree on the defaults.

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

# One definition for every script, after both files above, so a tunnel or the
# supervisor reads the port the board on THIS host was actually started with.
PORT="${CLAUDE_FLEET_PORT:-7879}"
# Where the supervisor and the tunnels keep pid files, logs and the tunnel URL.
RUN_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/claude-fleet"
# The board's virtualenv, on this machine's own disk rather than in the repo.
# The repo is often one NFS mount shared by several hosts: a venv there is
# built by one host's python for all of them, and every import the board does
# reads it over the network, so a slow server slows its start and every
# reload, and a wedged one stops them. run.sh creates it on first use.
VENV="${CLAUDE_FLEET_VENV:-${XDG_DATA_HOME:-$HOME/.local/share}/claude-fleet/venv}"

# wait_for TRIES DELAY cmd... — run cmd up to TRIES times, DELAY seconds apart,
# stopping at the first success; when none succeeds, check once more after the
# last pause. Returns cmd's last status, so it reads as the condition itself:
#   if wait_for 20 1 grep -q "started tunnel" "$LOG"; then ...
# For a condition that is a negation, wrap it in a function first.
wait_for() {
    local tries="$1" delay="$2" _
    shift 2
    for _ in $(seq "$tries"); do
        "$@" && return 0
        sleep "$delay"
    done
    "$@"
}

# http_code URL [MAX_TIME] — the HTTP status URL answers with, or 000 when
# nothing does (curl's own code for no response). MAX_TIME defaults to 5s.
http_code() {
    curl -s -o /dev/null -w '%{http_code}' --max-time "${2:-5}" "$1" 2>/dev/null || true
}

# board_code [MAX_TIME] — what an anonymous GET / gets from this host's board:
# 000 means nothing is listening, so one call answers "is it up" too.
board_code() {
    http_code "http://127.0.0.1:$PORT/" "${1:-5}"
}

# exec_board — replace this shell with the board's uvicorn on $PORT. Never
# returns. Called from the repo root; the board runs from $RUN_DIR instead. The
# repo is often an NFS mount, and every tmux the board runs resolves its working
# directory: on a congested NFS server each 2s refresh stalled on that.
exec_board() {
    local repo="$PWD"
    mkdir -p "$RUN_DIR" && cd "$RUN_DIR" || return
    exec "$VENV/bin/uvicorn" app:app --app-dir "$repo" --host 127.0.0.1 --port "$PORT"
}

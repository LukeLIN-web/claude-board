#!/usr/bin/env bash
# peer-tunnel.sh [start|stop|status] — keep the ssh forwards that this board's
# peer aggregation rides on (core/peers.py).
#
# WHY SSH AND NOT THE PEER'S LAN ADDRESS: every board binds 127.0.0.1 on
# purpose — it can type into tmux panes, so it is a shell, and a shell should
# not answer the office network. Forwarding a loopback port over ssh gives the
# aggregating board a local address to poll while the peer stays exactly as
# unreachable as before. The password gate (core/auth.py) still applies to the
# hop; FLEET_API_TOKEN from .env.local is what gets it through.
#
# Config, normally in .env.local.<hostname> next to FLEET_PEERS:
#   FLEET_PEER_TUNNELS="7880:hostb:7879 7881:hostc:7879"
# One entry per peer, in ssh -L order: <local port>:<ssh host>:<peer's port>.
# The local port is what FLEET_PEERS points at:
#   FLEET_PEERS=b=http://127.0.0.1:7880
#
# Each forward runs under a small supervisor loop, because a bare `ssh -N` dies
# with the network and takes the peer's cards with it silently. The loop
# reconnects; ServerAliveInterval is what makes a half-open link fail fast
# enough for that to matter.
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

RUN_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/claude-fleet"
TUNNELS="${FLEET_PEER_TUNNELS:-}"

# `ss` is the check that matters: a supervisor pid says a process exists, a
# listening port says the forward is actually usable.
listening() {
    ss -ltn "sport = :$1" 2>/dev/null | grep -q LISTEN
}

case "${1:-start}" in
stop)
    stopped=0
    for f in "$RUN_DIR"/peer-tunnel.*.pid; do
        [ -e "$f" ] || continue
        pid="$(cat "$f" 2>/dev/null)"
        # Kill the supervisor's whole process group, or the loop just respawns
        # the ssh we killed.
        [ -n "$pid" ] && kill -- "-$pid" 2>/dev/null && stopped=$((stopped + 1))
        rm -f "$f"
    done
    echo "[peer-tunnel] stopped $stopped"
    exit 0
    ;;
status)
    [ -n "$TUNNELS" ] || { echo "[peer-tunnel] FLEET_PEER_TUNNELS is not set — no peers to forward"; exit 0; }
    for spec in $TUNNELS; do
        lport="${spec%%:*}"
        if listening "$lport"; then
            echo "[peer-tunnel] $spec — up (127.0.0.1:$lport listening)"
        else
            echo "[peer-tunnel] $spec — DOWN (nothing listening on $lport)" >&2
        fi
    done
    exit 0
    ;;
# Internal: the supervisor loop, kept in this file so it shares the ssh options
# with the check above. Not for direct use.
__supervise)
    spec="$2"
    lport="${spec%%:*}"; rest="${spec#*:}"
    sshhost="${rest%%:*}"; rport="${rest##*:}"
    while true; do
        ssh -N -o ExitOnForwardFailure=yes \
            -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
            -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
            -L "$lport:127.0.0.1:$rport" "$sshhost"
        echo "[peer-tunnel] $spec dropped (exit $?), retrying in 5s"
        sleep 5
    done
    ;;
start) ;;
*)
    echo "usage: scripts/peer-tunnel.sh [start|stop|status]" >&2
    exit 2
    ;;
esac

# --- start ------------------------------------------------------------------
if [ -z "$TUNNELS" ]; then
    echo "error: FLEET_PEER_TUNNELS is not set — nothing to forward." >&2
    echo "       Set it in .env.local.$(hostname), e.g. \"7880:hostb:7879\"." >&2
    exit 2
fi

mkdir -p "$RUN_DIR"
for spec in $TUNNELS; do
    case "$spec" in
    *:*:*) ;;
    *) echo "[peer-tunnel] skipping malformed entry '$spec' (want <lport>:<host>:<rport>)" >&2; continue ;;
    esac
    lport="${spec%%:*}"
    if listening "$lport"; then
        echo "[peer-tunnel] $spec — already up"
        continue
    fi
    pidfile="$RUN_DIR/peer-tunnel.$lport.pid"
    setsid "$0" __supervise "$spec" >> "$RUN_DIR/peer-tunnel.log" 2>&1 < /dev/null &
    printf '%s\n' "$!" > "$pidfile"
    # ssh's handshake is a round trip; give it one before reporting.
    for _ in $(seq 10); do
        listening "$lport" && break
        sleep 1
    done
    listening "$lport" \
        && echo "[peer-tunnel] $spec — up" \
        || echo "[peer-tunnel] $spec — did not come up; see $RUN_DIR/peer-tunnel.log" >&2
done
exit 0

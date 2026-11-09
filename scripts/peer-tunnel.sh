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
# One entry per peer, in ssh -L order: <local port>:<ssh host>:<peer's port>,
# plus an optional :<via host> (below).
# The local port is what FLEET_PEERS points at:
#   FLEET_PEERS=b=http://127.0.0.1:7880
#
# FORWARDING THROUGH A HOST YOU ARE ALREADY LOGGED IN TO: a 4th field names a
# host whose ssh ControlMaster is already up, and the forward rides it instead
# of logging in again:
#   FLEET_PEER_TUNNELS="50957:boardhost:50956:gateway"
# One session on gateway's master forwards local 50957 to gateway's 50957 and,
# on gateway, runs `ssh -N -L 50957:127.0.0.1:50956 boardhost` — so the second
# hop uses gateway's own keys and ssh config, and the only new login is
# gateway → boardhost, inside that network. A gateway that bans an address for
# logging in too often never sees one. The master's socket is
# $FLEET_SSH_CONTROL_DIR/<gateway> when that is set, else what `ssh -G <gateway>`
# says its ControlPath is; with no master up the forward fails rather than
# logging in to gateway itself. The local port is also taken on gateway.
#
# Each forward runs under a small supervisor loop, because a bare `ssh -N` dies
# with the network and takes the peer's cards with it silently. The loop
# reconnects, backing off from 5 s to 5 min while it keeps failing (a link
# that held for a minute starts over at 5 s): retrying a refused login every
# few seconds is how an address gets banned. ServerAliveInterval is what makes
# a half-open link fail fast enough for reconnecting to matter.
#
# Exit codes: 0 ok · 2 usage/config
set -uo pipefail
cd "$(dirname "$0")/.."
# .env.local, then .env.local.<hostname>; sets RUN_DIR, defines wait_for.
source scripts/env.sh

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
        # TERM the supervisor, then the ssh or sleep it is waiting on: bash runs
        # its trap (which cancels a forward a master holds, see __supervise)
        # once that child is gone, and exits instead of respawning it.
        if [ -n "$pid" ] && kill -TERM "$pid" 2>/dev/null; then
            pkill -TERM -g "$pid" -x ssh 2>/dev/null
            pkill -TERM -g "$pid" -x sleep 2>/dev/null
            stopped=$((stopped + 1))
        fi
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
# Internal: the supervisor loop `start` runs detached, one per forward. Not for
# direct use.
__supervise)
    spec="$2"
    IFS=: read -r lport sshhost rport via <<< "$spec"
    # Without a via host: one ssh straight to the peer.
    cmd=(ssh -N -L "$lport:127.0.0.1:$rport" "$sshhost")
    if [ -n "$via" ]; then
        sock="${FLEET_SSH_CONTROL_DIR:+$FLEET_SSH_CONTROL_DIR/$via}"
        [ -n "$sock" ] || sock="$(ssh -G "$via" 2>/dev/null | awk '$1 == "controlpath" {print $2}')"
        if [ -z "$sock" ] || [ "$sock" = none ]; then
            echo "[peer-tunnel] $spec: no ControlPath for $via (set FLEET_SSH_CONTROL_DIR) — not logging in to it" >&2
            exit 2
        fi
        # ProxyCommand=/bin/false: with the master down this fails instead of
        # opening a login of its own. -tt ties the remote ssh's life to ours.
        # A forward asked for over a master belongs to the master: it outlives
        # this client and would hold the port against the next attempt. Cancel
        # it after every drop and on stop.
        cancel() { ssh -o ControlPath="$sock" -O cancel -L "$lport:127.0.0.1:$lport" "$via" 2>/dev/null; }
        trap 'cancel; exit 0' TERM INT
        cmd=(ssh -tt -o ControlPath="$sock" -o ControlMaster=no -o ProxyJump=none
             -o ProxyCommand=/bin/false -L "$lport:127.0.0.1:$lport" "$via"
             ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15
             -o BatchMode=yes -L "$lport:127.0.0.1:$rport" "$sshhost")
    fi
    delay=5
    while true; do
        started=$(date +%s)
        "${cmd[0]}" -o ExitOnForwardFailure=yes \
            -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
            -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
            "${cmd[@]:1}" < /dev/null
        rc=$?
        [ -n "$via" ] && cancel
        [ $(( $(date +%s) - started )) -ge 60 ] && delay=5
        echo "[peer-tunnel] $spec dropped (exit $rc), retrying in ${delay}s"
        sleep "$delay"
        delay=$(( delay * 2 > 300 ? 300 : delay * 2 ))
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
    *:*:*:*:*) echo "[peer-tunnel] skipping malformed entry '$spec' (want <lport>:<host>:<rport>[:<via>])" >&2; continue ;;
    *:*:*) ;;
    *) echo "[peer-tunnel] skipping malformed entry '$spec' (want <lport>:<host>:<rport>[:<via>])" >&2; continue ;;
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
    wait_for 10 1 listening "$lport" \
        && echo "[peer-tunnel] $spec — up" \
        || echo "[peer-tunnel] $spec — did not come up; see $RUN_DIR/peer-tunnel.log" >&2
done
exit 0

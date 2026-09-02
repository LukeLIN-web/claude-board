#!/usr/bin/env bash
# cf-tunnel.sh [start|stop|status|url] — publish the local board on a free
# Cloudflare quick tunnel (https://<random>.trycloudflare.com).
#
# HOW THIS DIFFERS FROM tunnel.sh (ngrok): there, the login lives at the edge in
# an ngrok traffic policy. A Cloudflare *quick* tunnel has no edge policy at all
# — it is a raw pipe from the public internet to this port — so the gate has to
# be inside the app (core/auth.py, enabled by FLEET_AUTH_PASSWORD in .env.local).
#
# Which moves the risk: the tunnel can no longer enforce anything, so this script
# does not trust configuration to tell it the gate is up. It PROBES the running
# board and refuses to start unless an anonymous request is actually rejected.
# That is deliberately stronger than reading FLEET_AUTH_PASSWORD here: run.sh
# starts uvicorn detached, so a password added to .env.local after the board came
# up is set in this shell and absent in the process being published. Reading the
# variable would say "gated"; asking the server says what is true.
#
# And because one check at start time only covers start time, `start` also
# leaves a watchdog running that repeats the probe and kills the tunnel if the
# board ever loses its gate (a restart without the password, say).
#
# Config in .env.local (gitignored):
#   FLEET_AUTH_PASSWORD  required — the gate; no password, no tunnel
#   FLEET_API_TOKEN      optional — bearer token for scripts hitting /api/*
#   CLAUDE_FLEET_PORT    local board port (default 7879, as in run.sh)
#
# The URL is random and changes on every start — quick tunnels carry no
# stability guarantee — so `start` prints it and `url` reprints it later.
#
# Exit codes: 0 ok · 2 usage/config · 3 board not running · 4 tunnel failed
#             · 5 board is running WITHOUT the gate (refusing to publish).
set -uo pipefail
cd "$(dirname "$0")/.."

if [ -f .env.local ]; then
    set -a; source .env.local; set +a
fi
# Same two-file layering run.sh uses, so this reads the port the board on THIS
# host was actually started with.
if [ -f ".env.local.$(hostname)" ]; then
    set -a; source ".env.local.$(hostname)"; set +a
fi

PORT="${CLAUDE_FLEET_PORT:-7879}"
RUN_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/claude-fleet"
LOG="$RUN_DIR/cf-tunnel.log"
URL_FILE="$RUN_DIR/cf-tunnel.url"
WATCHDOG_PID_FILE="$RUN_DIR/cf-tunnel.watchdog.pid"
WATCH_INTERVAL="${FLEET_TUNNEL_WATCH_INTERVAL:-30}"

# Ask the local board what an anonymous visitor gets. Echoes the status code;
# curl reports 000 when nothing is listening, so this answers "is the board up"
# and "is it gated" in one round trip. Used by `start`, `status` and the
# watchdog, so the probe has exactly one definition.
probe_gate() {
    curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$PORT/"
}

# The one place the accepted responses are written down. A gated board bounces
# an anonymous browser (303 to /login; 302 and 401 accepted so this does not
# have to be re-tuned when the gate's shape changes). Keeping this in a single
# function is the point: the local check, the through-the-tunnel check and the
# watchdog all have to agree, and a set that drifts between them would let the
# script refuse a board locally and then trust it through the edge.
# Verdicts: gated · ungated · down · odd
gate_verdict() {
    case "$1" in
    200)         echo ungated ;;
    303|302|401) echo gated ;;
    000)         echo down ;;
    *)           echo odd ;;
    esac
}

watchdog_running() {
    [ -s "$WATCHDOG_PID_FILE" ] && kill -0 "$(cat "$WATCHDOG_PID_FILE")" 2>/dev/null
}

stop_watchdog() {
    if watchdog_running; then kill "$(cat "$WATCHDOG_PID_FILE")" 2>/dev/null; fi
    rm -f "$WATCHDOG_PID_FILE"
}

# Match the process by name. A -f pattern would also match this script's own
# command line and kill the caller instead of the tunnel.
case "${1:-start}" in
stop)
    stop_watchdog
    if pkill -x cloudflared 2>/dev/null; then
        rm -f "$URL_FILE"
        echo "[cf-tunnel] stopped"
    else
        echo "[cf-tunnel] nothing running"
    fi
    exit 0
    ;;
# Internal: the supervisor started by `start`, kept in this file so it shares
# probe_gate/gate_verdict with the check it continues. Not for direct use.
__watchdog)
    while sleep "$WATCH_INTERVAL"; do
        # Tunnel gone ⇒ nothing left to guard. Exit rather than idle forever.
        pgrep -x cloudflared >/dev/null || exit 0
        if [ "$(gate_verdict "$(probe_gate)")" = "ungated" ]; then
            echo "[cf-tunnel] watchdog: board on 127.0.0.1:$PORT lost its gate — tearing the tunnel down" >&2
            pkill -x cloudflared
            rm -f "$URL_FILE"
            exit 5
        fi
    done
    ;;
url)
    if [ -s "$URL_FILE" ]; then cat "$URL_FILE"; exit 0; fi
    echo "[cf-tunnel] no URL recorded — is it running?" >&2
    exit 3
    ;;
status)
    if ! pgrep -x cloudflared >/dev/null; then echo "[cf-tunnel] not running"; exit 0; fi
    url="$(cat "$URL_FILE" 2>/dev/null)"
    echo "[cf-tunnel] running${url:+ -> $url}"
    # Check the gate before reporting anything reassuring. The watchdog should
    # have caught this already; if it is dead, this is the backstop.
    if [ "$(gate_verdict "$(probe_gate)")" = "ungated" ]; then
        echo "[cf-tunnel] DANGER: the board on 127.0.0.1:$PORT has NO gate and this tunnel is publishing it." >&2
        echo "            Stop it now (scripts/cf-tunnel.sh stop), set FLEET_AUTH_PASSWORD, restart the board." >&2
        exit 5
    fi
    watchdog_running && echo "[cf-tunnel] watchdog: active" \
        || echo "[cf-tunnel] watchdog: NOT running — nothing will tear the tunnel down if the board loses its gate" >&2
    if [ -n "$url" ]; then
        edge="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$url/")"
        case "$(gate_verdict "$edge")" in
        gated)   echo "[cf-tunnel] edge: anonymous request gets HTTP $edge (login) — gate is up" ;;
        down)    echo "[cf-tunnel] edge: could not reach $url from this host — see the DNS note in start" >&2 ;;
        *)       echo "[cf-tunnel] edge: DANGER — anonymous request got HTTP $edge, not a login" >&2; exit 5 ;;
        esac
    fi
    exit 0
    ;;
start) ;;
*)
    echo "usage: scripts/cf-tunnel.sh [start|stop|status|url]" >&2
    exit 2
    ;;
esac

# --- checks -----------------------------------------------------------------
command -v cloudflared >/dev/null 2>&1 || {
    echo "error: cloudflared not on PATH — https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/" >&2
    exit 2
}

# THE CHECK THIS SCRIPT EXISTS FOR, and the liveness check in the same request:
# an anonymous GET / must be rejected, and curl says 000 if nothing is serving.
# 200 means the board in front of us is ungated, and publishing it would put a
# shell that types into tmux panes on a public hostname.
code="$(probe_gate)"
case "$(gate_verdict "$code")" in
down)
    echo "error: nothing serving on 127.0.0.1:$PORT — start the board first (./run.sh)" >&2
    exit 3
    ;;
ungated)
    echo "error: the board on 127.0.0.1:$PORT answered an anonymous request with HTTP 200 — it has no password gate." >&2
    echo "       Set FLEET_AUTH_PASSWORD in .env.local and RESTART the board (./run.sh), then retry." >&2
    echo "       A restart is the part that is easy to miss: run.sh runs uvicorn detached, so" >&2
    echo "       editing .env.local does nothing to the process already serving this port." >&2
    exit 5
    ;;
odd)
    echo "error: unexpected HTTP $code from an anonymous request to the board; refusing to publish." >&2
    exit 5
    ;;
esac

# --- start ------------------------------------------------------------------
mkdir -p "$RUN_DIR"
umask 077
stop_watchdog
pkill -x cloudflared 2>/dev/null && sleep 1
rm -f "$URL_FILE"

setsid cloudflared tunnel --url "http://127.0.0.1:$PORT" \
    --no-autoupdate \
    > "$LOG" 2>&1 < /dev/null &

# cloudflared prints the assigned hostname into its startup banner. Poll for it
# rather than sleeping a fixed amount: the handshake is usually a couple of
# seconds but is a network round trip, not a constant.
url=""
for _ in $(seq 30); do
    url="$(grep -om1 'https://[a-z0-9-]*\.trycloudflare\.com' "$LOG" 2>/dev/null)"
    [ -n "$url" ] && break
    sleep 1
done

if [ -z "$url" ]; then
    echo "[cf-tunnel] failed to get a URL; last lines of $LOG:" >&2
    tail -10 "$LOG" >&2
    exit 4
fi

printf '%s\n' "$url" > "$URL_FILE"
echo "[cf-tunnel] up -> $url (password required)"

# Verify through the public edge, not just locally: this is the only check that
# covers the whole path, and a tunnel pointed at the wrong port would still have
# passed every test above.
#
# The pause before the first attempt is not politeness — it is the difference
# between this check working and this host being unable to see its own tunnel
# for minutes. The hostname is seconds old, so an immediate lookup gets NXDOMAIN,
# and a resolver that caches negatives will keep serving that NXDOMAIN for its
# negative TTL long after Cloudflare is answering for the name. Probing too
# early doesn't just fail; it poisons the answer for every later probe.
sleep 5
edge=""
for _ in $(seq 12); do
    edge="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$url/")"
    [ "$edge" != "000" ] && break
    sleep 5
done

case "$(gate_verdict "$edge")" in
gated)
    echo "[cf-tunnel] verified: anonymous request through the tunnel gets HTTP $edge (login)"
    ;;
down)
    # The tunnel is up and the local gate already passed; only this host cannot
    # resolve the name — usually a cached negative lookup, which other machines
    # (your phone) do not share. Say that precisely rather than implying either
    # that the board is exposed or that the tunnel is broken.
    echo "[cf-tunnel] NOTE: $url does not resolve from this host yet." >&2
    echo "          The local gate check passed and the tunnel is up, so the board is published and protected —" >&2
    echo "          this machine just has a stale negative DNS entry. Other devices are usually unaffected." >&2
    echo "          Confirm from here with:  curl --resolve $(printf '%s' "${url#https://}"):443:\$(dig +short ${url#https://} @1.1.1.1 | head -1) $url/" >&2
    ;;
*)
    echo "[cf-tunnel] DANGER: anonymous request through the tunnel got HTTP $edge, not a login." >&2
    echo "            Stopping the tunnel rather than leaving it published." >&2
    pkill -x cloudflared 2>/dev/null
    rm -f "$URL_FILE"
    exit 5
    ;;
esac

# --- supervise ---------------------------------------------------------------
# The check above is a snapshot. Restarting the board without FLEET_AUTH_PASSWORD
# while this tunnel is live would publish an ungated board, and the start-time
# refusal cannot see that — `run.sh` restarts are routine, so this is a plausible
# accident, not a theoretical one. The watchdog re-runs the same probe every
# WATCH_INTERVAL seconds and tears the tunnel down the moment the gate goes
# away, which turns "hope someone runs status" into an invariant.
setsid "$0" __watchdog >> "$LOG" 2>&1 < /dev/null &
printf '%s\n' "$!" > "$WATCHDOG_PID_FILE"
echo "[cf-tunnel] watchdog: probing every ${WATCH_INTERVAL}s; tunnel drops if the gate does"
exit 0

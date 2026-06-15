#!/usr/bin/env bash
# locate-session.sh <session-id-or-prefix> — resolve a Claude Code session id
# to its tmux pane, without needing the fleet server.
#
# Claude Code natively registers every live session in ~/.claude/sessions/
# (<pid>.json with {pid, sessionId, cwd, ...}), so the chain is just:
#   session id -> pid -> tty (ps) -> tmux pane (#{pane_tty} match)
# Falls back to the SessionStart-hook map (~/.claude/session-map/) for ids
# whose pid file is gone. Prints one JSON object on success; exits 1 with a
# message on stderr otherwise. Prefixes must be >= 8 chars and unique.
set -u
HOME_BASE="${CLAUDE_FLEET_HOME:-$HOME}"
SESS_DIR="$HOME_BASE/.claude/sessions"
MAP_DIR="$HOME_BASE/.claude/session-map"

sid="${1:-}"
if [ -z "$sid" ]; then
  echo "usage: locate-session.sh <session-id-or-prefix(>=8 chars)>" >&2
  exit 1
fi
sid=$(printf '%s' "$sid" | tr 'A-Z' 'a-z')
if [ "${#sid}" -lt 8 ]; then
  echo "error: prefix too short (need >= 8 chars): $sid" >&2
  exit 1
fi

emit() { # pid sessionId cwd transcript tty pane target source
  jq -n --arg pid "$1" --arg sid "$2" --arg cwd "$3" --arg transcript "$4" \
        --arg tty "$5" --arg pane "$6" --arg target "$7" --arg src "$8" \
    '{session_id:$sid, pid:(if $pid=="" then null else ($pid|tonumber) end),
      cwd:$cwd, transcript_path:$transcript, tty:$tty,
      tmux_pane:(if $pane=="" then null else $pane end),
      tmux_target:(if $target=="" then null else $target end), source:$src}'
}

pane_for_tty() {
  tmux list-panes -a -F '#{pane_id}	#{pane_tty}' 2>/dev/null \
    | awk -F'\t' -v tty="$1" '$2 == tty { print $1; exit }'
}

# --- primary: native ~/.claude/sessions/<pid>.json ---
matches=()
if [ -d "$SESS_DIR" ]; then
  while IFS= read -r f; do
    full=$(jq -r '.sessionId // empty' "$f" 2>/dev/null | tr 'A-Z' 'a-z')
    case "$full" in "$sid"*) matches+=("$f") ;; esac
  done < <(find "$SESS_DIR" -name '*.json' ! -name 'session-*' 2>/dev/null)
fi

live=()
for f in ${matches[@]+"${matches[@]}"}; do
  pid=$(jq -r '.pid // empty' "$f")
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && live+=("$f")
done

if [ "${#live[@]}" -gt 1 ]; then
  echo "error: ambiguous prefix '$sid' matches ${#live[@]} live sessions:" >&2
  for f in "${live[@]}"; do jq -r '"  \(.sessionId)  pid=\(.pid)  \(.cwd)"' "$f" >&2; done
  exit 1
fi

if [ "${#live[@]}" -eq 1 ]; then
  f="${live[0]}"
  pid=$(jq -r '.pid' "$f")
  full=$(jq -r '.sessionId' "$f")
  cwd=$(jq -r '.cwd // ""' "$f")
  # mirror Claude Code's project-dir naming: / _ . all become -
  slug=$(printf '%s' "$cwd" | sed 's#[/._]#-#g')
  transcript="$HOME_BASE/.claude/projects/$slug/$full.jsonl"
  [ -e "$transcript" ] || transcript=""
  tty=$(ps -o tty= -p "$pid" 2>/dev/null | tr -d ' ')
  pane="" target=""
  if [ -n "$tty" ] && [ "$tty" != "?" ]; then
    pane=$(pane_for_tty "/dev/$tty")
    [ -n "$pane" ] && target=$(tmux display-message -p -t "$pane" \
      '#{session_name}:#{window_index}.#{pane_index}' 2>/dev/null)
  fi
  emit "$pid" "$full" "$cwd" "$transcript" "${tty:+/dev/$tty}" "$pane" "$target" "claude-sessions-dir"
  exit 0
fi

# --- fallback: SessionStart-hook map (best-effort hints) ---
for f in "$MAP_DIR/$sid.json" $(ls "$MAP_DIR"/"$sid"*.json 2>/dev/null); do
  [ -e "$f" ] || continue
  pane=$(jq -r '.tmux_pane // empty' "$f")
  # only trust the hint if the pane still exists
  if [ -n "$pane" ] && tmux display-message -p -t "$pane" '' >/dev/null 2>&1; then
    target=$(tmux display-message -p -t "$pane" \
      '#{session_name}:#{window_index}.#{pane_index}' 2>/dev/null)
    emit "" "$(jq -r '.session_id' "$f")" "$(jq -r '.cwd // ""' "$f")" \
         "$(jq -r '.transcript_path // ""' "$f")" "" "$pane" "$target" "session-map-hook"
    exit 0
  fi
done

echo "error: no live session matches '$sid'" >&2
exit 1

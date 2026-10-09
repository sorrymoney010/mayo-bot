#!/bin/bash
# Mac cold-standby failover for the mayo-bot PAPER loop. NOT INSTALLED BY DEFAULT.
# Runs every 300 s from launchd (com.mayo.failover.plist). No AI, no keys, read-only GitHub
# access (unauthenticated GET of the box's secret heartbeat gist; 12 req/h << 60/h limit).
#
#   box last_active_ts older than 30 min (box dead / paper not cycling) -> START Mac paper
#   box state=returning, or box active and fresh                        -> STOP Mac paper
#   heartbeat not readable (Mac offline, GitHub down, bad JSON)         -> NO CHANGE
#   box state=user_stop (someone touched logs/BOX_STOP on the box)      -> NO CHANGE
#
# Never-both: the Mac only starts after the box has not been active for 30 min; the box,
# when it comes back after >20 min inactive, holds its paper start for 20 min publishing
# state=returning (box_heartbeat.sh gate) so this job stops the Mac first; with FENCE=1 the
# box also stops itself after 20 min of failed publishes, before the Mac could start.
set -u
CONF="$HOME/.mayo-failover.conf"          # GIST_ID=<id>   (no secrets)
LOG="$HOME/mayo-bot/logs/mac_failover.log"
LABELS=(com.mayo.kraken.paper com.mayo.kraken.watchdog)
TAKEOVER=1800       # s since box last_active_ts before the Mac takes over
FRESH=900           # s: box heartbeat this fresh + active => box owns the book
U=$(id -u)
mkdir -p "$(dirname "$LOG")"
log() { echo "$(date '+%F %T %Z') $*" >> "$LOG"; }
GIST_ID=$(sed -n 's/^GIST_ID=\([0-9a-fA-F]\{8,64\}\)$/\1/p' "$CONF" 2>/dev/null | head -1)
[[ -n "$GIST_ID" ]] || { log "no GIST_ID in $CONF; doing nothing"; exit 0; }

json=$(curl -fsS --max-time 20 -H 'Accept: application/vnd.github+json' \
  "https://api.github.com/gists/$GIST_ID" 2>/dev/null) || { log "heartbeat fetch failed; no change"; exit 0; }
body=$(osascript -l JavaScript -e 'function run(a){try{return JSON.parse(a[0]).files["box_heartbeat.txt"].content}catch(e){return ""}}' "$json" 2>/dev/null)
kv() { sed -n "s/^$1=//p" <<<"$body" | head -1; }
box_ts=$(kv box_ts); state=$(kv state); last_active=$(kv last_active_ts)
[[ "$box_ts" =~ ^[0-9]+$ && "$last_active" =~ ^[0-9]+$ && -n "$state" ]] || { log "heartbeat unparsable; no change"; exit 0; }
now=$(date +%s); hb_age=$(( now - box_ts )); act_age=$(( now - last_active ))

mac_running() { launchctl print "gui/$U/com.mayo.kraken.paper" >/dev/null 2>&1; }
start_mac() {
  local l; for l in "${LABELS[@]}"; do
    launchctl enable "gui/$U/$l"
    launchctl bootstrap "gui/$U" "$HOME/Library/LaunchAgents/$l.plist" 2>/dev/null || true
  done
}
stop_mac() {
  local l; for l in com.mayo.kraken.watchdog com.mayo.kraken.paper; do
    launchctl bootout "gui/$U/$l" 2>/dev/null || true
    launchctl disable "gui/$U/$l"
  done
}

if [[ "$state" == user_stop ]]; then
  :   # box paper deliberately stopped (logs/BOX_STOP): never auto-start the Mac
elif [[ "$state" == returning ]] || { [[ "$state" == active ]] && (( hb_age < FRESH )); }; then
  if mac_running; then stop_mac; log "STOP Mac paper: box state=$state hb_age=${hb_age}s (box owns the book)"; fi
elif (( act_age > TAKEOVER )); then
  if ! mac_running; then start_mac; log "START Mac paper: box last active ${act_age}s ago (state=$state hb_age=${hb_age}s)"; fi
fi
exit 0

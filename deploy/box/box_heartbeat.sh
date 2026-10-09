#!/usr/bin/env bash
# OPTIONAL box -> Mac failover heartbeat. INERT unless $MAYO_RUNTIME/heartbeat.conf exists
# (that file lives outside the repo and holds no secret: just GIST_ID=<secret gist id> and
# optionally FENCE=1). Publishing uses the box's EXISTING `gh` login (gist scope) via
# `gh api`; this script never reads or prints a token.
#   publish -> (health loop, every 300 s) write box_heartbeat.txt to the gist
#   gate    -> (paper supervisor, before each launch) 0 = OK to start the box paper loop;
#              1 = hold: the box was not "active" for >30 min, so the Mac standby may own the
#              paper book; publish state=returning and wait HOLD_SECONDS so the Mac stops first
#   status  -> print what would be published
# Mac side: deploy/mac-failover/ (README there). Thresholds: Mac starts its loop when the
# box's last_active_ts is >30 min old; box self-fences (FENCE=1 only) after 20 min of failed
# publishes, i.e. BEFORE the Mac could start -> never two ledger owners at once.
set -u
source /workspace/mayo-bot/deploy/box/box_env.sh
CONF="$MAYO_RUNTIME/heartbeat.conf"
HLOG=${MAYO_HB_LOG:-$REPO/logs/box_heartbeat.log}
FENCED=${MAYO_FENCE_FILE:-$REPO/logs/BOX_FENCED}     # paper supervisor treats it like BOX_STOP
PLOCK=${MAYO_PAPER_LOCK:-$REPO/logs/paper_trader.lock}
GH=${MAYO_GH:-gh}                                  # override only for tests
ST="$MAYO_RUNTIME/heartbeat"          # state dir (last_ok, last_active, hold_until)
TAKEOVER_SECONDS=1800; GATE_SAFE_SECONDS=1200; FENCE_SECONDS=1200; HOLD_SECONDS=${HOLD_SECONDS:-1200}
[[ -f "$CONF" ]] || exit 0
GIST_ID=$(sed -n 's/^GIST_ID=\([0-9a-fA-F]\{8,64\}\)$/\1/p' "$CONF" | head -1)
FENCE=$(sed -n 's/^FENCE=\([01]\)$/\1/p' "$CONF" | head -1); FENCE=${FENCE:-0}
[[ -n "$GIST_ID" ]] || { blog "$HLOG" "heartbeat.conf has no valid GIST_ID; inert"; exit 0; }
mkdir -p "$ST"
now=$(date +%s)
rd() { cat "$ST/$1" 2>/dev/null || echo 0; }

compose() {  # key=value lines (trivial for the Mac to parse with grep/cut)
  local pp n age state hold
  pp=$(pgrep -f -- "$PAPER_PROC_RE" | tr '\n' ' '); n=$(wc -w <<<"$pp")
  age=$(( now - $(stat -c %Y "$REPO/logs/paper_trader.log" 2>/dev/null || echo 0) ))
  hold=$(rd hold_until)
  if (( n == 1 && age < 900 )) && [[ ! -f "$REPO/logs/BOX_STOP" && ! -f "$FENCED" ]]; then state=active
  elif [[ -f "$REPO/logs/BOX_STOP" ]]; then state=user_stop     # deliberate stop: Mac must not take over
  elif (( hold > 0 )); then state=returning
  else state=stopped; fi
  echo "schema=1"; echo "box_ts=$now"; echo "box_iso=$(date -Is)"; echo "state=$state"
  echo "paper_procs=$n"; echo "paper_log_age_s=$age"; echo "hold_until=$hold"
  echo "last_active_ts=$([[ $state == active ]] && echo "$now" || rd last_active)"
}

publish() {
  local body payload
  body=$(compose)
  payload=$(BODY="$body" /usr/bin/python3 -c 'import json,os; print(json.dumps({"files":{"box_heartbeat.txt":{"content":os.environ["BODY"]+"\n"}}}))')
  if env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/home/box timeout 30 \
       "$GH" api -X PATCH "gists/$GIST_ID" --input - <<<"$payload" >/dev/null 2>>"$HLOG"; then
    echo "$now" > "$ST/last_ok"
    grep -q '^state=active$' <<<"$body" && echo "$now" > "$ST/last_active"
    if [[ -f "$FENCED" ]]; then rm -f "$FENCED"; blog "$HLOG" "publish OK again; unfenced"; fi
    return 0
  fi
  local last_ok; last_ok=$(rd last_ok)
  blog "$HLOG" "publish FAILED (last ok $(( now - last_ok ))s ago)"
  if [[ "$FENCE" == 1 ]] && (( last_ok > 0 && now - last_ok > FENCE_SECONDS )) && [[ ! -f "$FENCED" ]]; then
    touch "$FENCED"
    local p; p=$(cat "$PLOCK" 2>/dev/null)
    [[ -n "$p" ]] && kill -0 "$p" 2>/dev/null && kill -TERM "$p"
    blog "$HLOG" "FENCED: cannot publish for >${FENCE_SECONDS}s; stopped paper pid=${p:-none} so the Mac can take over safely"
  fi
  return 1
}

gate() {
  local last_active hold
  last_active=$(rd last_active); hold=$(rd hold_until)
  if (( hold == 0 && last_active > 0 && now - last_active < GATE_SAFE_SECONDS )); then return 0; fi
  if (( hold == 0 )); then
    hold=$(( now + HOLD_SECONDS )); echo "$hold" > "$ST/hold_until"
    blog "$HLOG" "gate: box not active for >${GATE_SAFE_SECONDS}s (Mac may own the book); holding paper start until $(date -d @"$hold" '+%F %T %Z')"
  fi
  publish || true
  if (( now < hold )); then return 1; fi
  if [[ "$FENCE" == 1 ]] && (( $(rd last_ok) < hold - HOLD_SECONDS )); then
    blog "$HLOG" "gate: hold over but no successful publish during it (FENCE=1) -> keep holding"; return 1
  fi
  rm -f "$ST/hold_until"; blog "$HLOG" "gate: hold over; box paper may start"; return 0
}

case "${1:-status}" in
  publish) publish ;;
  gate) gate ;;
  status) compose ;;
  *) echo "usage: $0 publish|gate|status" >&2; exit 2 ;;
esac

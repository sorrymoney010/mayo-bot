# Mac standby failover (designed, NOT installed)

Goal: if the box paper loop is gone for >30 min (box restarted/moved and no shell opened,
crash, ...) the Mac starts its paper loop automatically, and hands the book back when the box
returns, with no AI and no new credentials. Never both at once.

## How it works
1. Box (`deploy/box/box_heartbeat.sh`, called by the health loop every 300 s) PATCHes one file,
   `box_heartbeat.txt`, in a **secret gist** using the box's existing `gh` login
   (account sorrymoney010, scopes gist/repo/read:org; no new token). Content (no secrets):
   `box_ts`, `state` (active | returning | stopped | user_stop), `last_active_ts`, `paper_procs`,
   `paper_log_age_s`, `hold_until`.
2. Mac (`mac_failover.sh`, launchd every 300 s) GETs `https://api.github.com/gists/<id>`
   unauthenticated (secret gist = readable only by someone who knows the id), and:
   - `last_active_ts` older than 30 min -> enable + bootstrap com.mayo.kraken.paper/.watchdog
   - `state=returning`, or `active` with a heartbeat < 15 min old -> bootout + disable them
   - `state=user_stop` (logs/BOX_STOP on the box) or heartbeat unreadable -> no change
3. Box coming back after >20 min not active: the paper supervisor's `gate` holds the start
   for 20 min, publishing `state=returning`; the Mac stops within <= 5 min; then the box starts.
4. Optional `FENCE=1`: if the box cannot publish for 20 min it touches logs/BOX_FENCED and
   TERMs its own paper loop (the supervisor won't restart it; cleared on the next good publish).
   That way a box that can't reach GitHub is down before the Mac's 30 min takeover.
   Tradeoff: a GitHub outage then stops box paper too. Without FENCE, a box that is up but
   can't reach GitHub for >30 min could overlap with the Mac.

Ledgers: box and Mac each keep their own book in their own logs/; a failover period produces
fills on the Mac book. Reconcile by hand afterwards if needed (nothing is copied automatically).

## Exact steps (need the user's OK; nothing below has been run)
On the box:
  cd /workspace/mayo-bot
  echo "state=pending" > /tmp/box_heartbeat.txt                     # placeholder; first publish overwrites it
  gh gist create --desc "mayo box heartbeat" /tmp/box_heartbeat.txt  # secret by default; prints URL
  echo "GIST_ID=<id from the URL>" > /workspace/mayo-runtime/heartbeat.conf
  # optional: echo "FENCE=1" >> /workspace/mayo-runtime/heartbeat.conf
  deploy/box/box_heartbeat.sh publish && tail logs/box_heartbeat.log
On the Mac (after pulling this branch into ~/mayo-bot):
  echo "GIST_ID=<same id>" > ~/.mayo-failover.conf
  sed "s#__HOME__#$HOME#g" ~/mayo-bot/deploy/mac-failover/com.mayo.failover.plist \
     > ~/Library/LaunchAgents/com.mayo.failover.plist
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mayo.failover.plist
  tail -f ~/mayo-bot/logs/mac_failover.log
Uninstall: `launchctl bootout gui/$(id -u)/com.mayo.failover; rm ~/Library/LaunchAgents/com.mayo.failover.plist`
and on the box `rm /workspace/mayo-runtime/heartbeat.conf` (heartbeat + gate + fence go inert).
Requirements on the Mac: curl, osascript, launchctl (all stock macOS). The Mac's own
com.mayo.kraken.paper plist must keep forcing PAPER_TRADING=true/DRY_RUN=true (unchanged).

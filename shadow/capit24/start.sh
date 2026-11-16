#!/bin/bash
# Start the CAPIT-24 shadow loop unless it is already running (signal log only; no orders, no keys).
# Idempotent: a live loop.pid whose cmdline is capit24/loop.sh, or any other capit24/loop.sh process,
# means "already running" and nothing is launched. flock stops two start.sh racing.
# A manual start clears NO_AUTOSTART (set by stop.sh). Stop: shadow/capit24/stop.sh
D=/workspace/mayo-bot/shadow/capit24
exec 9>"$D/start.lock"
flock -n 9 || { echo "another start.sh is running; skipped"; exit 0; }
P=$(cat "$D/loop.pid" 2>/dev/null)
if [ -n "$P" ] && kill -0 "$P" 2>/dev/null && grep -q capit24/loop.sh /proc/"$P"/cmdline 2>/dev/null; then
  echo "already running pid $P"; exit 0; fi
P=$(pgrep -f -u "$(id -u)" 'shadow/capit24/loop.sh' | head -1)
if [ -n "$P" ]; then echo "already running pid $P (loop.pid stale)"; exit 0; fi
[ "$1" = "--auto" ] || rm -f "$D/NO_AUTOSTART"
cd "$D" || exit 1
exec 9>&-   # release the start lock; the pgrep check covers the window before the pid file appears
nohup setsid env -i PATH=/usr/bin:/bin HOME=/home/box /bin/bash "$D/loop.sh" >> "$D/run.log" 2>&1 < /dev/null &
sleep 2; echo "started pid $(cat "$D/loop.pid" 2>/dev/null)"

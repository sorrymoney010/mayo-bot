#!/bin/bash
# Start the CAPIT-24L PAPER runner (main plan) unless it is already running. Paper only: no keys, no .env, env -i.
# Idempotent: a live runner.pid whose cmdline is capit24l_paper.py, or any other capit24l_paper.py --loop
# process, means "already running" and nothing is launched. flock stops two start.sh racing.
# A manual start clears NO_AUTOSTART (set by stop.sh). Stop: paper/capit24l_binanceus/stop.sh. NOT in capit_autostart.sh yet (owner OK needed).
D=/workspace/mayo-bot/paper/capit24l_binanceus
PY=/workspace/mayo-runtime/pyenv/bin/python
[ -x "$PY" ] || PY=/usr/bin/python3
exec 9>"$D/start.lock"
flock -n 9 || { echo "another start.sh is running; skipped"; exit 0; }
P=$(cat "$D/runner.pid" 2>/dev/null)
if [ -n "$P" ] && kill -0 "$P" 2>/dev/null && grep -q capit24l_paper.py /proc/"$P"/cmdline 2>/dev/null; then
  echo "already running pid $P"; exit 0; fi
P=$(pgrep -f -u "$(id -u)" 'scripts/capit24l_paper.py --loop' | head -1)
if [ -n "$P" ]; then echo "already running pid $P (runner.pid stale)"; exit 0; fi
[ "$1" = "--auto" ] || rm -f "$D/NO_AUTOSTART"
cd "$D" || exit 1
exec 9>&-   # release the start lock; the pgrep check covers the window before the pid file appears
nohup setsid env -i PATH=/usr/bin:/bin HOME=/home/box TZ=America/Chicago \
  "$PY" /workspace/mayo-bot/scripts/capit24l_paper.py --loop >> "$D/run.log" 2>&1 < /dev/null &
sleep 3; echo "started pid $(cat "$D/runner.pid" 2>/dev/null)"

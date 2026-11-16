#!/bin/bash
# Start the CAPIT-24 PAPER runner unless it is already running. Paper only: no keys, no .env, env -i.
# Stop: paper/capit_binanceus/stop.sh
D=/workspace/mayo-bot/paper/capit_binanceus
PY=/workspace/mayo-runtime/pyenv/bin/python
[ -x "$PY" ] || PY=/usr/bin/python3
if [ -f "$D/runner.pid" ] && kill -0 "$(cat "$D/runner.pid")" 2>/dev/null \
   && grep -q capit_paper.py /proc/"$(cat "$D/runner.pid")"/cmdline 2>/dev/null; then
  echo "already running pid $(cat "$D/runner.pid")"; exit 0; fi
cd "$D" && nohup setsid env -i PATH=/usr/bin:/bin HOME=/home/box TZ=America/Chicago \
  "$PY" /workspace/mayo-bot/scripts/capit_paper.py --loop >> "$D/run.log" 2>&1 < /dev/null &
sleep 3; echo "started pid $(cat "$D/runner.pid" 2>/dev/null)"

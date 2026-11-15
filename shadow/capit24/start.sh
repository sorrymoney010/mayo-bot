#!/bin/bash
# Start the CAPIT-24 shadow loop unless it is already running. Stop: kill $(cat loop.pid)
D=/workspace/mayo-bot/shadow/capit24
if [ -f "$D/loop.pid" ] && kill -0 "$(cat "$D/loop.pid")" 2>/dev/null && grep -q capit24/loop.sh /proc/"$(cat "$D/loop.pid")"/cmdline 2>/dev/null; then
  echo "already running pid $(cat "$D/loop.pid")"; exit 0; fi
cd "$D" && nohup setsid env -i PATH=/usr/bin:/bin HOME=/home/box /bin/bash "$D/loop.sh" >> "$D/run.log" 2>&1 < /dev/null &
sleep 2; echo "started pid $(cat "$D/loop.pid")"

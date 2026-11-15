#!/bin/bash
# CAPIT-24 shadow loop: one cycle every 15 min. Signal log only, no orders, no keys.
D=/workspace/mayo-bot/shadow/capit24
echo $$ > "$D/loop.pid"
trap 'rm -f "$D/loop.pid"' EXIT
while true; do
  env -i PATH=/usr/bin:/bin HOME=/home/box python3 /workspace/mayo-bot/research/edge2/capit_shadow.py
  sleep 900
done

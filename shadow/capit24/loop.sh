#!/bin/bash
# CAPIT-24 shadow loop: one cycle every 15 min. Signal log only, no orders, no keys.
# Wait in <=30s wall-clock slices so a box sleep/hibernate cannot stall the next cycle.
D=/workspace/mayo-bot/shadow/capit24
INTERVAL=900
SLICE=30
echo $$ > "$D/loop.pid"
trap 'rm -f "$D/loop.pid"' EXIT
while true; do
  env -i PATH=/usr/bin:/bin HOME=/home/box python3 /workspace/mayo-bot/research/edge2/capit_shadow.py
  target=$(( $(date +%s) + INTERVAL ))
  while [ "$(date +%s)" -lt "$target" ]; do
    rem=$(( target - $(date +%s) ))
    [ "$rem" -le 0 ] && break
    if [ "$rem" -gt "$SLICE" ]; then sl=$SLICE; else sl=$rem; fi
    sleep "$sl"
  done
done

#!/bin/bash
# Stop the CAPIT-24 shadow loop (only this process) and set NO_AUTOSTART so the auto-restart hook
# (scripts/capit_autostart.sh) leaves it stopped. start.sh clears it.
D=/workspace/mayo-bot/shadow/capit24
touch "$D/NO_AUTOSTART"
P=$(cat "$D/loop.pid" 2>/dev/null)
if [ -n "$P" ] && grep -q capit24/loop.sh /proc/"$P"/cmdline 2>/dev/null; then kill "$P" && echo "stopped $P"; else echo "not running"; fi

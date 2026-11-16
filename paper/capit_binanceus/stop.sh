#!/bin/bash
# Stop the CAPIT-24 PAPER runner (only this process; nothing else) and set NO_AUTOSTART so the
# reboot/auto-restart hook (scripts/capit_autostart.sh) leaves it stopped. start.sh clears it.
D=/workspace/mayo-bot/paper/capit_binanceus
touch "$D/NO_AUTOSTART"
P=$(cat "$D/runner.pid" 2>/dev/null)
if [ -n "$P" ] && grep -q capit_paper.py /proc/"$P"/cmdline 2>/dev/null; then kill "$P" && echo "stopped $P"; else echo "not running"; fi

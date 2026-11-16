#!/bin/bash
# Stop the CAPIT-24 PAPER runner (only this process; nothing else).
D=/workspace/mayo-bot/paper/capit_binanceus
P=$(cat "$D/runner.pid" 2>/dev/null)
if [ -n "$P" ] && grep -q capit_paper.py /proc/"$P"/cmdline 2>/dev/null; then kill "$P" && echo "stopped $P"; else echo "not running"; fi

#!/bin/bash
cd "/Users/musicmancheef/Documents/Codex/2026-08-03/referenced-chatgpt-conversation-this-is-an-2/Dublin-local"
while true; do
  PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m dublin_bot.cli dashboard >> logs/dashboard.log 2>&1
  echo "[$(date)] dashboard exited code $? -- restarting in 2s" >> logs/dashboard.log
  sleep 2
done

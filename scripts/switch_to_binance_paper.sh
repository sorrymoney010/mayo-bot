#!/bin/bash
# One-shot: move the PAPER bot from Kraken to Binance.US. Paper only — this
# script never enables live trading and needs no API key.
set -uo pipefail
cd "${MAYO_ROOT:-$HOME/mayo-bot}" || { echo "mayo-bot folder not found"; exit 1; }
ROOT=$(pwd)
PY="$ROOT/.venv/bin/python"
say() { printf '\n== %s\n' "$*"; }

say "1/5 Getting the Binance.US code"
git fetch origin claude/youthful-keller-p7vt92 || { echo "git fetch failed"; exit 1; }
if ! git checkout claude/youthful-keller-p7vt92; then
  echo "Local changes block the checkout; saving them with git stash and retrying."
  git stash push -m "before-binance-switch $(date +%F)" && git checkout claude/youthful-keller-p7vt92 || exit 1
fi
git pull --ff-only origin claude/youthful-keller-p7vt92 || true

say "2/5 Backing up logs/"
if [ -d logs ]; then
  BK="logs-kraken-backup-$(date +%F-%H%M%S)"
  cp -R logs "$BK" && echo "saved to $BK"
fi

say "3/5 Pointing the bot at Binance.US (.env + paper fees)"
touch .env
if grep -q '^BROKER=' .env; then sed -i '' 's/^BROKER=.*/BROKER=binance/' .env; else echo 'BROKER=binance' >> .env; fi
grep -q '^BINANCE_HOST=' .env || echo 'BINANCE_HOST=binance.us' >> .env
# Binance.US standard tier (approx.). Adjust to your actual tier if lower.
sed -i '' 's/^export PAPER_TAKER_FEE_BPS=.*/export PAPER_TAKER_FEE_BPS=60/; s/^export PAPER_MAKER_FEE_BPS=.*/export PAPER_MAKER_FEE_BPS=40/' scripts/run_paper_mac.sh
grep -E '^(BROKER|BINANCE_HOST)=' .env
grep FEE_BPS scripts/run_paper_mac.sh

say "4/5 Checking safety locks and the Binance.US connection"
PAPER_TRADING=true DRY_RUN=true ALLOW_LIVE_TRADING=false "$PY" -m dublin_bot.cli doctor
"$PY" -m dublin_bot.cli health

say "5/5 Restarting the paper loop"
pkill -f paper_trader_loop.py && sleep 3
nohup "$ROOT/scripts/run_paper_mac.sh" >> "$ROOT/logs/paper_loop_stdout.log" 2>&1 &
echo "paper loop started (pid $!); waiting 30s for the first cycle..."
sleep 30
tail -n 30 logs/paper_trader.log
say "Done. Paper only on Binance.US. Copy everything above back to Claude."

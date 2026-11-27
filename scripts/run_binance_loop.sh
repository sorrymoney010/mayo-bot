#!/usr/bin/env bash
# Run one cycle every 15 minutes (the strategy acts only on closed 4h bars; extra cycles are harmless).
# Stop with Ctrl+C. The exchange-side stop keeps protecting an open position while this is not running.
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p logs
while true; do
  echo "[$(date)] cycle" >> logs/binance_loop.log
  bash scripts/run_binance.sh run-once >> logs/binance_loop.log 2>&1 || echo "[$(date)] cycle exited non-zero" >> logs/binance_loop.log
  sleep 900
done

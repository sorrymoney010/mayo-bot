#!/bin/bash
set -euo pipefail
cd /Users/musicmancheef/mayo-bot
# PAPER ONLY. Force the safety locks via process env (overrides .env).
export PAPER_TRADING=true
export DRY_RUN=true
export ALLOW_LIVE_TRADING=false
export LIVE_RISK_ACKNOWLEDGEMENT=
# Strategy picked by walk-forward (docs/WALKFORWARD.md, data/walkforward_report.txt):
# regime-switch trend sleeve on 1h bars — ADX/vol gated, flat in chop, chandelier exit.
# Edge is marginal and outlier-dependent; this is a paper experiment, not a proven edge.
export STRATEGY=regime_trend
export TIMEFRAME_MINUTES=60
export REGIME_LOOKBACK=20
export REGIME_ATR_MULT=3.0
export REGIME_MIN_ATR_RANK=0.0
export STOP_LOSS_PCT=0.03
export TAKE_PROFIT_PCT=0.25
# Paper budget: $500 book sized from the paper ledger, not the real Kraken balance.
export STRATEGY_EQUITY_USD=500
export PAPER_USE_LEDGER_EQUITY=true
export RISK_PER_TRADE=0.01
# Paper fill model = Kraken tier-1 (taker 0.40%/side, maker 0.25%/side) + 10bps slippage.
export PAPER_TAKER_FEE_BPS=40
export PAPER_MAKER_FEE_BPS=25
# Adaptive learner gate (bench negative-expectancy symbol/regime after 8 closed trades).
export LEARNER_GATE_ENABLED=true
export LEARNER_MIN_SAMPLE=8
export LEARNER_BENCH_HOURS=72
# Unset any mangled list envs so Settings reads clean JSON from .env file
unset COIN_BASKET BREAKOUT_SYMBOLS UNIVERSE_ALLOWLIST LIVE_READY_UNIVERSE
exec /Users/musicmancheef/mayo-bot/.venv/bin/python /Users/musicmancheef/mayo-bot/scripts/paper_trader_loop.py

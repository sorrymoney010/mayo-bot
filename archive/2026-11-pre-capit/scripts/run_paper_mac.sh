#!/bin/bash
set -euo pipefail
cd /Users/musicmancheef/mayo-bot
# PAPER ONLY. Force the safety locks via process env (overrides .env).
export PAPER_TRADING=true
export DRY_RUN=true
export ALLOW_LIVE_TRADING=false
export LIVE_RISK_ACKNOWLEDGEMENT=
# This Mac is the ONLY paper-ledger writer; any other copy refuses to start.
export MAYO_LEDGER_OWNER=1
# Paper never calls private Kraken endpoints (book equity comes from the ledger).
export PAPER_BLOCK_PRIVATE_API=true
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
export PAPER_SLIPPAGE_BPS=10
# Adaptive learner gate (bench negative-expectancy symbol/regime after 8 closed trades).
export LEARNER_GATE_ENABLED=true
export LEARNER_MIN_SAMPLE=30
export LEARNER_BENCH_HOURS=72
# Second PAPER sleeve: 4h mean reversion with post-only limit entries on
# BTC/ETH/SOL (walk-forward meanrev_mk@240m; small sample — experiment only).
# Shares the $500 book, max 3 positions and the risk caps with regime_trend.
export MEANREV_SLEEVE_ENABLED=true
# Daily risk-on filter (close_d > SMA50_d, SMA50 rising 5d) on ENTRIES of both
# sleeves; never forces exits (data/d1_trendhold_report.txt).
export REGIME_DAILY_FILTER=true
export MEANREV_DAILY_FILTER=true
# Third PAPER sleeve: 4h trend-hold (D1 on, close>EMA100, EMA20>EMA100; exit
# first 4h close < EMA100; no stop), 25% of the book per coin. One position per
# coin across ALL sleeves; max 3 positions; total exposure cap 75% (was 50%:
# raised only so three 25% positions fit — per-position cap stays 25%).
export TRENDHOLD_SLEEVE_ENABLED=true
export TRENDHOLD_POSITION_FRACTION=0.25
export MAX_CONCURRENT_POSITIONS=3
export MAX_POSITION_FRACTION=0.25
export MAX_EXPOSURE_FRACTION=0.75
# Market-data pipeline: bars for BTC/ETH/SOL come from the local tick store
# (written by com.mayo.kraken.ticks / scripts/tick_collector.py) with Kraken
# REST OHLC as fallback for missing/short/stale history. docs/PIPELINE.md.
export PIPELINE_ENABLED=true
export PIPELINE_DATA_DIR=/Users/musicmancheef/mayo-bot/data
export PIPELINE_STALE_SECONDS=600
# Unset any mangled list envs so Settings reads clean JSON from .env file
unset COIN_BASKET BREAKOUT_SYMBOLS UNIVERSE_ALLOWLIST LIVE_READY_UNIVERSE PIPELINE_SYMBOLS
exec /Users/musicmancheef/mayo-bot/.venv/bin/python /Users/musicmancheef/mayo-bot/scripts/paper_trader_loop.py

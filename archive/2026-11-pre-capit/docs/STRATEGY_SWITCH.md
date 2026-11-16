# Switching paper strategies

Live trading stays locked: keep `PAPER_TRADING=true`, `DRY_RUN=true`, `ALLOW_LIVE_TRADING=false`.

## Available `STRATEGY` values

| Value | Behavior |
| --- | --- |
| `momentum` (default) | RSI band entry with **hard** regime EMA gate (no counter-trend BUYs). In-position exit below slow EMA unchanged. |
| `mean_reversion` | Oversold stretch + reversion exit. |
| `sr_flip` | Swing-pivot resistance→support flip reclaim (see `strategies/sr_flip_strategy.py`). |
| `pattern` / `elliott_lite` | Rule-based double-bottom neckline break and bullish flag breakout (see `strategies/pattern_strategy.py`). |
| `breakout` / `momentum_breakout` | N-bar high + volume > N-bar avg (see `strategies/breakout_strategy.py`). Defined-risk: ~1% equity, stop 2%, TP 4% (2R). Multi-symbol scan of `BREAKOUT_SYMBOLS` (default BTC/ETH/SOL) with max concurrent 3 / max 1 per symbol. |
| `regime_trend` | ADX/vol-gated trend follower, flat in chop, chandelier + ADX-chop exits, multi-symbol like breakout. Walk-forward pick on 1h (see `docs/WALKFORWARD.md`). Adaptive learner gate benches losing symbols/regimes. |

## How to switch (paper loop)

1. Set in `.env` (or export for one restart):

```bash
STRATEGY=breakout   # or sr_flip | pattern | elliott_lite | momentum | mean_reversion
TIMEFRAME_MINUTES=15
PAPER_TRADING=true
DRY_RUN=true
ALLOW_LIVE_TRADING=false
```

2. Restart the paper loop so the new factory selection loads:

```bash
kill "$(cat logs/paper_trader.pid)"
PAPER_TRADING=true DRY_RUN=true ALLOW_LIVE_TRADING=false \
  nohup .venv/bin/python scripts/paper_trader_loop.py >> logs/paper_trader.log 2>&1 &
echo $! > logs/paper_trader.pid
```

Optional knobs (also in `.env.example`): `SR_*` for `sr_flip`, `PATTERN_*` for pattern/elliott_lite,
`BREAKOUT_*` / `STOP_LOSS_PCT` / `TAKE_PROFIT_PCT` / `MAX_CONCURRENT_POSITIONS` for breakout.

## Breakout sleeve tuning

| Env | Default | Meaning |
| --- | --- | --- |
| `BREAKOUT_LOOKBACK` | 20 | N-bar prior high (excludes current bar) |
| `BREAKOUT_VOL_AVG` | 20 | Volume must exceed this many bars' average |
| `STOP_LOSS_PCT` / `STOP_PCT` | 0.02 for sleeve | Paper/live stop fraction below entry |
| `TAKE_PROFIT_PCT` | 0.04 for sleeve | Paper/live take-profit fraction (2R) |
| `RISK_PER_TRADE` / `RISK_PCT` | 0.01 | ~1% equity risked to the stop |
| `BREAKOUT_SYMBOLS` | BTC/ETH/SOL | Multi-symbol scan universe (canonical `/USD` form) |
| `MAX_CONCURRENT_POSITIONS` | 3 | Cap on open breakout lots (1 per symbol) |
| `MAX_DAILY_LOSS_FRACTION` | 0.03 | Existing daily-loss circuit breaker (reused) |
| `TIMEFRAME_MINUTES` | 15 | Bar timeframe for this sleeve |

**ADX choice:** soft-disabled for `breakout` / `momentum_breakout`. The engine ADX sit-out
runs only when `STRATEGY=momentum`. Breakouts prioritize volume + high break over ADX.
Fee min-edge still applies; a 4% TP clears `MIN_EDGE_BPS=100`.

**Multi-symbol:** the paper engine scans `BREAKOUT_SYMBOLS` each cycle, prefers stop/TP
exits on held lots, then opens a new breakout when under the concurrent cap. Paper ledger
(`paper_bot_positions.json` / `paper_portfolio.json`) already supports multiple symbols.

## ADX sit-out + fee min-edge (momentum)

Momentum entries are blocked in chop and when the take-profit cannot clear fees:

| Env | Default | Meaning |
| --- | --- | --- |
| `ADX_PERIOD` | 14 | Wilder ADX lookback on OHLCV bars |
| `ADX_ENTER_ABOVE` | 25 | Allow new BUYs when ADX rises above this |
| `ADX_EXIT_BELOW` | 20 | Sit out when ADX falls below this |
| `ADX_GATE_ENABLED` | true | Hard gate for momentum BUYs |
| `MIN_EDGE_BPS` | 100 | Min TP / expected move in bps before fees (~1.0%) |
| `MIN_EDGE_GATE_ENABLED` | true | Block micro targets vs Kraken Tier-1 fees |

Between `ADX_EXIT_BELOW` and `ADX_ENTER_ABOVE` the prior allow/deny state is kept (hysteresis). Logged reasons look like `ADX sit-out: chop` and `Fee gate: TP … < min edge …`.

Live stays locked: never set `ALLOW_LIVE_TRADING=true` for this work.

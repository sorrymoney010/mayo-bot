# Dublin Trading OS

A private, paper-first crypto trading system built around strict risk controls,
transparent decisions, and testable strategies with a native Kraken Spot connector.

> **Safety status: fully locked.** `PAPER_TRADING=true`, `DRY_RUN=true`,
> `ALLOW_LIVE_TRADING=false`. The bot cannot place a real order.
> See [SAFETY.md](SAFETY.md) before changing anything.

## Documentation

| Document | Purpose |
|---|---|
| [SAFETY.md](SAFETY.md) | Safety model, residual risks, pre-live checklist |
| [RUNBOOK.md](RUNBOOK.md) | Daily operation, incident response, state files |
| [docs/WALKFORWARD.md](docs/WALKFORWARD.md) | Fee-aware walk-forward backtest, adaptive learner, current paper config |
| [docs/MEANREV_SLEEVE.md](docs/MEANREV_SLEEVE.md) | 4h mean-reversion paper sleeve (limit entries) running beside regime_trend |
| [docs/PAPER_PRO.md](docs/PAPER_PRO.md) | Trailing take-profit, fast exits, paper futures/margin, strategy research |

## Current scope

- **Kraken Spot** native connector (Alpaca retained as fallback)
- BTC/USD first, with a reusable multi-asset architecture
- $25 strategy budget by default; dollar-notional sizing
- Conservative risk defaults: `RISK_PER_TRADE=0.01` (alias `RISK_PCT`),
  `MAX_POSITION_FRACTION=0.25`, `MAX_LEVERAGE=1.0` (sizer cap, spot only),
  `MARGIN_EXPOSURE_FRACTION=0`
- Paper loop runs two sleeves on one paper book: `regime_trend@1h` plus a 4h
  mean-reversion sleeve with post-only limit entries on BTC/ETH/SOL
  (see docs/MEANREV_SLEEVE.md). Shared max-positions and risk caps, and no
  symbol collisions between sleeves
- No leverage on the spot book, **no withdrawal access**. A paper perpetual
  short and a paper margin-cost model exist and default **off**; see
  [docs/PAPER_PRO.md](docs/PAPER_PRO.md). Live US access to Kraken margin and
  futures depends on account eligibility.
- Trend + breakout confirmation with ATR-based risk sizing
- Daily loss, drawdown, cooldown, and order-count circuit breakers
- Market-data freshness validation and exchange clock-skew detection
- Exchange precision and minimum-order enforcement (`Decimal`, round-down)
- Strictly monotonic, restart-safe nonces; client-side rate limiting
- Persistent idempotency ledger preventing duplicate orders
- Hash-chained, tamper-evident audit log
- Crash recovery for in-flight orders
- Mobile PWA dashboard with connection-health and freshness indicators

## The gate pipeline

Every cycle runs nine gates in order; any failure aborts before an order forms:

```
safety locks → market data → freshness → market quality → strategy
   → risk → precision → idempotency → execution
```

## Setup on macOS

```bash
git clone https://github.com/sorrymoney010/mayo-bot.git
cd mayo-bot
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
dublin-bot doctor
dublin-bot run-once
```

The default `run-once` command is dry-run paper trading and cannot submit a real
order. Public market data works without credentials. If you later add a Kraken
key for private account-status checks, use a **read-only** key (*Query Funds* +
*Query Orders* only — never *Withdraw Funds*). Never commit `.env`.

## Commands

```bash
dublin-bot doctor         # config + safety summary (no secrets)
dublin-bot health         # reachability, latency, clock skew, rate budget
dublin-bot freshness      # bar age / clock-skew verdict (exit 2 if stale)
dublin-bot pairs          # resolved pair metadata: precision, minimums
dublin-bot run-once       # one full gated cycle
dublin-bot recover        # resolve pending intents after a crash
dublin-bot audit-verify   # verify the audit hash chain (exit 3 if broken)
dublin-bot safety-report  # full pre-live review bundle
dublin-bot dashboard      # http://127.0.0.1:8765
```

The dashboard binds only to localhost and refuses to start unless the safety
locks are engaged.

## Tests

```bash
ruff check .     # lint the complete project
pytest -q        # run the complete offline test suite
```

Current verified result: the offline suite (`pytest -q`) plus `ruff check .`.
`tests/test_safety_locks.py` fails loudly if the trading locks are relaxed.
`tests/test_paper_pro.py` fails if the futures or margin live locks can fire
while any existing lock is still shut.

## Paper professional features (defaults)

| Feature | Default | Notes |
|---|---|---|
| Trailing take-profit, per sleeve | **off** | 2026-10-03 public-OHLC walk-forward did not improve out-of-sample results after fees. See docs/PAPER_PRO.md |
| Fast exit watcher (stops / trailing / TP) | **on** | Inside the paper loop; public prices; no new entries |
| Futures short sleeve (`PF_XBTUSD`, `PF_ETHUSD`, `PF_SOLUSD`) | **off** | 2026-10-03 study: 0 one-hour shorts, 4 four-hour shorts at −164 bps. Leverage hard-capped at 2x |
| `ALLOW_FUTURES_LIVE_ORDERS` / `ALLOW_MARGIN_LIVE_ORDERS` | **off** | Also require the three safety locks to be opened |
| Spot-margin short sleeve | not built | Borrow cost is modeled; perpetuals are the short. See docs/PAPER_PRO.md |
| `scripts/strategy_research.py` | manual | Walk-forward on 1h+ bars; winners register as shadow sleeves (no fills) |

## Important

This software does not guarantee profit. Paper fills are simulated and can
differ from live fills, especially for low-liquidity assets. Cheap coin price
alone is not an edge; liquidity, spread, volatility, and execution quality
matter more. The strategy has **no demonstrated edge on Kraken data** — see
residual risk #4 in SAFETY.md.

# Dublin Trading OS (mayo-bot)

A private, **paper-only** crypto trading system built around strict risk controls.

> **Safety status: fully locked.** `PAPER_TRADING=true`, `DRY_RUN=true`,
> `ALLOW_LIVE_TRADING=false`, futures/margin live flags off. The bot cannot place a real order.
> See [SAFETY.md](SAFETY.md) before changing anything.

## Current state (15 Nov 2026) — one strategy, paper only, not validated forward

After two months of strategies that did not survive fees, the repo was reset to **one** strategy:

- **CAPIT-24 crash-buy**, frozen: buy the next 1h open after a coin's 24h drop is ≥ 2.5× its
  normal 24h move on ≥ 2× normal volume; sell 48 h later. Book: **VET, HBAR, ALGO, XLM**, max two
  $50 slots on $100 of paper equity.
- **Venue for the paper book: Binance.US** (public data; fees 0% maker / 0.02% taker, modeled as
  10 bps round trip with slip). A **Kraken 80 bps** shadow book tracks the same trades so we see
  what it would cost there.
- **Evidence so far is a backtest only** (2022–2026, 78 trades, PF 2.82 at 10 bps / 2.32 at
  80 bps; fails at Kraken taker 160 bps). There are **zero closed forward trades** yet. It is not
  proven and it is not live.
- **Kill rules:** after 30 closed trades with PF < 1.10 at the stated fee → kill; two consecutive
  net-negative quarters while below cash → kill.
- Live trading stays locked until the owner gives an explicit per-flag OK and adds his own keys.

| Where | What |
|---|---|
| [paper/capit_binanceus/README.md](paper/capit_binanceus/README.md) | Frozen config, fee books, kill rules, how to run |
| `src/dublin_bot/capit24.py`, `scripts/capit_paper.py` | The rule and the hourly paper runner |
| [reports/low_price_coins_study_2026-11-15.md](reports/low_price_coins_study_2026-11-15.md) | Why these four coins, why not PUMP, why not "cheap" coins |
| [reports/edge_search_2026-11-14.md](reports/edge_search_2026-11-14.md) | How CAPIT-24 was found (and its weaknesses) |
| [reports/venue_fees_2026-11-15.md](reports/venue_fees_2026-11-15.md) | US exchange fees for $100 trades |
| [archive/2026-11-pre-capit/README.md](archive/2026-11-pre-capit/README.md) | Everything retired on 15 Nov 2026, and why |
| `shadow/capit24/` | Separate research shadow of CAPIT-24 on the old 12-coin set (signal log only) |

## Documentation

| Document | Purpose |
|---|---|
| [SAFETY.md](SAFETY.md) | Safety model, residual risks, pre-live checklist |
| [RUNBOOK.md](RUNBOOK.md) | Daily operation, incident response, state files (parts describe the retired loop) |
| [docs/WALKFORWARD.md](docs/WALKFORWARD.md) | Fee-aware walk-forward backtest, adaptive learner |
| [docs/PAPER_PRO.md](docs/PAPER_PRO.md) | Trailing take-profit, fast exits, paper futures/margin |

## Legacy engine (dormant shared infrastructure)

Everything below describes the gated Kraken engine (`engine.py`, CLI, dashboard). It is kept
because its safety locks, exchange clients and tests are shared infrastructure. **No strategy
runs on it now**; the old multi-sleeve paper loop was archived.

### Engine scope

- **Kraken Spot** native connector (Alpaca retained as fallback)
- BTC/USD first, with a reusable multi-asset architecture
- $25 strategy budget by default; dollar-notional sizing
- Conservative risk defaults: `RISK_PER_TRADE=0.01` (alias `RISK_PCT`),
  `MAX_POSITION_FRACTION=0.25`, `MAX_LEVERAGE=1.0` (sizer cap, spot only),
  `MARGIN_EXPOSURE_FRACTION=0`
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
| Fast exit watcher (stops / trailing / TP) | **on** | `EXIT_WATCHER_ENABLED=false` stops it without a code change. Public prices; no new entries |
| Futures short sleeve (`PF_XBTUSD`, `PF_ETHUSD`, `PF_SOLUSD`) | **off** | 2026-10-03 study: 0 one-hour shorts, 4 four-hour shorts at −164 bps. Leverage hard-capped at 2x |
| `ALLOW_FUTURES_LIVE_ORDERS` / `ALLOW_MARGIN_LIVE_ORDERS` | **off** | Also require the three safety locks to be opened |
| Spot-margin short sleeve | not built | Borrow cost is modeled; perpetuals are the short. See docs/PAPER_PRO.md |
| `scripts/strategy_research.py` | weekly via `research_cycle.py` | Walk-forward on 1h+ bars + order-flow overlay study; winners register as shadow (no fills) |
| `scripts/research_cycle.py` | box_health (hourly check) | Weekly research + daily forward shadow scoring; staged candidate -> shadow -> paper promotion of order-flow overlays |
| `scripts/study_orderflow_exits.py` | manual | Order-flow entry filters / exit timers on the live sleeves (walk-forward, after fees) |

## Important

This software does not guarantee profit. Paper fills are simulated and can
differ from live fills, especially for low-liquidity assets. Cheap coin price
alone is not an edge; liquidity, spread, volatility, and execution quality
matter more. The strategy has **no demonstrated edge on Kraken data** — see
residual risk #4 in SAFETY.md.

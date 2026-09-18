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

## Current scope

- **Kraken Spot** native connector (Alpaca retained as fallback)
- BTC/USD first, with a reusable multi-asset architecture
- $25 strategy budget by default; dollar-notional sizing
- No leverage, no shorting, **no withdrawal access**
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
git clone https://github.com/sorrymoney010/Dublin-.git
cd Dublin-
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

Current verified result: **225 tests passed**. The suite is fully offline — no
network calls, no real orders.
`tests/test_safety_locks.py` fails loudly if the trading locks are relaxed.

## Important

This software does not guarantee profit. Paper fills are simulated and can
differ from live fills, especially for low-liquidity assets. Cheap coin price
alone is not an edge; liquidity, spread, volatility, and execution quality
matter more. The strategy has **no demonstrated edge on Kraken data** — see
residual risk #4 in SAFETY.md.

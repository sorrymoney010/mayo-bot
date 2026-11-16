# MAYO — four-coin Binance limit-entry evidence contract

**2026-11-16 · Research and paper-only preparation · issue #19.** This branch is separate from the blocked BTC/ETH/SOL PR #18. No exchange orders, no deploy/restart, no change to the running paper ledger, no live permissions.

## Verified vs reported
- **Operator/Nova runner report:** VET, HBAR, ALGO, XLM, no BTC execution and no PUMP.
- **Coder-reported candidate** ("Capit 24 L", precise rules not yet independently supplied): passive buy at **0.995 × signal candle's completed close**, 58 historical trades since Aug 2022, reported profit factor 2.47 after fees and ~3.1% mean per trade.
- **Evidence gaps:** approximately 530 variants explored, four coins selected after seeing results, prior Binance-priced backtest may not have modeled a real maker fill. None of those statistics have been reproduced here.
- **Venue:** Explicitly distinguish `binance_us` from `binance_com` and `USD` from `USDT`. Account eligibility and fee tier cannot be assumed. Take maker/taker rate, tick size, min notional, history and quote currency from the exact **operating venue**. Fee schedules can change; record effective date. Paper prices are not exchange fills.

## Frozen first question
**For the unmodified candidate's signals, how often could a resting buy at 0.5% below the already-closed signal candle have filled before its original expiry, using subsequent venue-native public trade prints?**

The read-only command answers only this first question. It does **not** invent exit rules, daily performance, filled quantities, maker queue priority, P&L, or a profitable edge.

## Inputs and command
Prepare three CSV files from the **same venue and quote**, with UTC Unix **milliseconds**; one market per `symbol` suffixed by `USD` or `USDT`:
- Signals: `signal_id,venue,symbol,close_ms,close_price,expires_ms`. Export a **frozen, timestamped signal file** from the coder's completed strategy. Signal IDs must be stable and unique. Only completed signal candles. The `expires_ms` must come from the *original* proposed order TTL, not invented from later prices.
- Public trade prints: `venue,symbol,trade_ms,price`. Include every required print within each time window, not only favorable observations.
- Feed-coverage manifest: `venue,symbol,start_ms,end_ms,gap_count`. Each interval must cover the full evaluation window for that market and certify zero gaps; otherwise result is unknown.

Example: `python scripts/research_limit_fill_audit.py --signals signals.csv --prints prints.csv --coverage coverage.csv --venue binance_us --quote USD > limit_evidence.json`

The script only prints JSON. `coverage_unknown` means missing or incomplete tick coverage; `no_touch` means no eligible public print at or below price; `touch_only` means a print exactly equals limit (queue fill unknown); `traded_through` means a later public print went *below* the resting buy limit (stronger but still NOT proof our order would have filled). A print at the signal close or at/after expiry **never counts**. The script sets `confirmed_fill=false` in every case. **These are opportunity classifications, not an execution simulator.**

It does not read `.env`, request API secrets or call private APIs. This tool has no code path to submit an order or write MAYO's ledger.

## Required next-stage analysis by coder (not supplied by this PR)
1. Supply the exact original candle timeframe, signal generator, expiry, cancellation, protection/stop, exits and sizing; pin the frozen strategy version and first locked date.
2. Validate public prints' continuous provenance and a venue-specific fill model: queue-aware maker assumptions and adverse stress scenarios. Distinguish *touch* from *trade-through* and apply conservative uncertainty; include missed trades and adverse selection.
3. Run the unmodified candidate on **untouched future data** (or an independently sequestered chronological holdout that no part of the 530-variant search or coin selection ever saw). If all historical data has been seen, label it discovery and collect forward paper results instead.
4. Pair/venue-specific round-trip maker/taker fees, any payment-token discounts, conversion, min notional, lot precision, and spread must be versioned by effective date. Do **not** transfer Kraken tier assumptions to Binance.
5. Show per-coin and aggregate signal count, submitted simulated limits, touch/trade-through/miss rates, completed paper trades, fees, net return, median, profit factor, maximal drawdown, time-in-market, tail concentration and comparable baseline/buy-and-hold. Reconcile every reported statistic against raw trade IDs.
6. Compare baseline and candidate at matching dates, capital, fill assumptions, and market exposures. Freeze the decision rule **before** new observations. At least 30 genuinely forward out-of-sample closed paper trades with PF >=1.10 after all costs, robustness and acceptable drawdown are a minimum evaluation threshold—not a live-trading approval.
7. Integration stays on its **own** read-only/shadow worker and namespaced ledger after code review. The existing strategy's paper book, running process, keys, live/futures/margin locks and any legacy execution history are untouched.

## Local tests
`python -m unittest tests.test_research_limit_fill_audit -v` (or `pytest -q tests/test_research_limit_fill_audit.py`). These tests assert no same-candle/after-expiry fills, true touch vs trade-through, gap/coverage failures, allowable coins, venue separation, and that the tool does not claim confirmed fills or profit.

**This PR adds an audit tool and fixtures/tests only. It does not include the original strategy, Binance data, a finished backtest, a launched paper worker, or proven alpha.**

# CAPIT-24 paper runner — Binance.US fees, Kraken 80 bps shadow

**PAPER ONLY.** No orders, no API keys, no `.env`, public market data only. Nothing here can
change `PAPER_TRADING`, `DRY_RUN` or `ALLOW_LIVE_TRADING`. Not validated forward yet.

Code: `src/dublin_bot/capit24.py` (rule + runner), `scripts/capit_paper.py` (CLI / hourly loop),
tests: `tests/test_capit24.py`.

## Frozen config (do not retune mid-test)

| Item | Value |
|---|---|
| Rule | On each closed 1h bar: z24 = 24h log return / (trailing-720h σ of 1h log returns, excl. bar, × √24) ≤ **−2.5** AND 24h quote volume ≥ **2×** trailing-30d average 24h volume |
| Entry / exit | Buy at the **open of the next 1h bar**; sell at the open **48 h** later. No stop, no target |
| Book | **VET, HBAR, ALGO, XLM** (fixed). Max **2** positions, 1 per coin, ties → most negative z |
| Size | $100 paper equity; each slot = 50% of start = **$50** notional (non-compounded, same as the backtest) |
| Data | `api.binance.us/api/v3/klines` 1h — price from `<COIN>USDT`, volume = USDT + USD pair quote volume |
| Primary fee book `capit24_binanceus` | 0.02% taker each side + 3 bps slip each side = **10 bps** round trip (Binance.US is 0% maker / 0.02% taker) |
| Shadow fee book `capit24_kraken80_shadow` | Kraken tier-1 0.40% maker each side = **80 bps** round trip (same signals and fills) |
| Also logged per trade | 0 bps (Binance.US maker), 4 bps (taker, no slip), 160 bps (Kraken taker) |
| Start | Forward only from the first cycle (no backfilled trades). R1 (BTC daily close > SMA200) is logged on each signal as a pre-specified variant, never a switch |

## Kill rules (checked every cycle, per fee book; a primary kill latches and stops new entries)

1. **K1** — after **30** closed trades, profit factor **< 1.10** at that book's fee → **KILL**.
2. **K2** — the last **two completed calendar quarters** are both net negative **and** the
   cumulative realized book is **below cash** (equity < $100) → **KILL**.

Pass (for any talk of real money, which also needs the owner's explicit per-flag OK and his
own keys): ≥ 30 closed trades, PF ≥ 1.10 after fees, majority of quarterly folds positive,
beats cash and buy-and-hold of the same four coins. Backtest reference (2022-01-01 → 2026-11-15,
78 trades): PF 2.82 at 10 bps (14/20 folds), PF 2.32 at 80 bps (13/20), **fails** fold majority at
160 bps (10/20) — see `reports/low_price_coins_study_2026-11-15.md`.

## Run

```bash
paper/capit_binanceus/start.sh     # background: nohup setsid env -i ... capit_paper.py --loop
paper/capit_binanceus/stop.sh
env -i PATH=/usr/bin:/bin HOME=$HOME /workspace/mayo-runtime/pyenv/bin/python scripts/capit_paper.py --once
```

Runtime files here (gitignored): `status.json` (latest cycle, open positions, per-coin z/volr,
kill-rule state), `state.json` (cursor + open positions), `signals.jsonl`, `closed_trades.jsonl`
(one row per closed trade per fee book, stable `trade_id`, readable by `dublin_bot.promotion`),
`equity.jsonl`, `events.jsonl`, `promotion_report.json`, `run.log`, `runner.pid`.

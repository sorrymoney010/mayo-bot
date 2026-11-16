# TREND-6 paper runner: Binance.US, BTC + ETH daily trend

**PAPER ONLY. Owner-approved and started 2026-11-16.** No orders, no API keys, no `.env`, public
data only. Nothing here can change `PAPER_TRADING`, `DRY_RUN` or `ALLOW_LIVE_TRADING`.
Venue: **Binance.US only**. Kraken columns are reference only, and Coinbase is never used.

Plan: [docs/MAIN_PLAN.md](../../docs/MAIN_PLAN.md). Verification: [reports/trend6_verification_2026-11-16.md](../../reports/trend6_verification_2026-11-16.md).
Code: `src/dublin_bot/trend6.py`, `scripts/trend6_paper.py`. Tests: `tests/test_trend6_paper.py`.

| Item | Value |
|---|---|
| Coins | BTC, ETH (Binance.US `BTCUSD`, `ETHUSD` 1d klines) |
| Decision | after each 00:00 UTC close: close > SMA50/100/200, 7/14/28-day return > 0 |
| Size | 50% × (yes/6) of book equity per coin; rest cash |
| Fill | open of the next day (Binance.US 1d open; fallback first 1h open) |
| Band | trade a coin only when its target changes; skip < $1 notional (logged) |
| SMA warm-up | Binance.US contiguous history; if < 201 days (2023-07..2025-02 gap), earlier closes from Binance global `data.binance.vision` archives. Each decision logs `data.source` |
| Books | `binanceus` (primary, 8 bps RT), `binanceus10` (10 bps), `ref_kraken80`, `ref_kraken160` (reference only) |
| Kills | primary book, latched (no new buys; exits still run). **K-DD**: max DD > 45% any time. **K-REL** (replaced PF < 1.10 on 2026-11-16): once ≥ 30 closed round trips, kill if return < 50/50 B&H return since start AND equity > 10% below peak AND equity below 30 days ago. Inputs in `status.json` → `kill_inputs`. History check: [docs/MAIN_PLAN.md](../../docs/MAIN_PLAN.md) §2 |
| Benchmark | 50/50 BTC/ETH bought at the first fill open, held |
| Start | forward only: the first decision is the latest closed day |
| Cycle | hourly, ~180 s past each UTC hour (wall-clock sliced sleep; catches up after box sleep) |

```bash
paper/trend6_binanceus/start.sh    # idempotent, env -i, never sources .env
paper/trend6_binanceus/stop.sh     # writes NO_AUTOSTART
env -i PATH=/usr/bin:/bin HOME=$HOME /workspace/mayo-runtime/pyenv/bin/python scripts/trend6_paper.py --once --out /tmp/t6
```

Runtime files here (gitignored): `status.json`, `state.json`, `decisions.jsonl` (every day: six
signals, weights, data source, fill open), `fills.jsonl`, `skipped.jsonl`, `equity.jsonl` (all books +
benchmark at each open), `events.jsonl`, `run.log`, `runner.pid`.

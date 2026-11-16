# CAPIT-24L paper runner: Binance.US, limit entry (MAIN PLAN)

**PAPER ONLY. NOT STARTED YET** (needs the owner's OK). No orders, no API keys, no `.env`, public
Binance.US market data only. Nothing here can change `PAPER_TRADING`, `DRY_RUN` or
`ALLOW_LIVE_TRADING`.

Plan and rules: [docs/MAIN_PLAN_CAPIT24L_BINANCEUS.md](../../docs/MAIN_PLAN_CAPIT24L_BINANCEUS.md).
Code: `src/dublin_bot/capit24l.py`, `scripts/capit24l_paper.py`; tests: `tests/test_capit24l.py`.

| Item | Value |
|---|---|
| Coins | VET, HBAR, ALGO, XLM |
| Signal | closed 1h bar: z24 ≤ −2.5 and 24h volume ≥ 2× normal (same as CAPIT-24) |
| Entry | post-only limit at 0.995 × signal close, live 60 min; fill only if a 1m low goes **strictly below**; touches logged, not filled |
| Exit | market, 48 h after the fill (1m open) |
| Book | $100, 2 × $50, one per coin; no new entries at equity ≤ $70 |
| Fee books | `binanceus_l` 5 bps (primary), `binanceus10` 10 bps, `kraken125` 125 bps |
| Kills | K1 PF < 1.10 after 30; K2 two negative quarters below cash; K4 first 15 average < −100 bps |
| Start | forward only from the first cycle (`--seed-cursor` is for scratch-dir replays only) |
| Cycle | hourly, ~150 s after each 1h close (wall-clock sliced sleep; catches up after box sleep) |

```bash
paper/capit24l_binanceus/start.sh    # idempotent
paper/capit24l_binanceus/stop.sh     # writes NO_AUTOSTART
env -i PATH=/usr/bin:/bin HOME=$HOME /workspace/mayo-runtime/pyenv/bin/python scripts/capit24l_paper.py --once
```

Runtime files here (gitignored): `status.json` (cycle, open positions, fill counts, books, kill and
floor state), `state.json`, `signals.jsonl` (every signal with its limit evaluation),
`closed_trades.jsonl` (one row per trade per fee book), `equity.jsonl`, `events.jsonl`,
`promotion_report.json`, `run.log`, `runner.pid`.

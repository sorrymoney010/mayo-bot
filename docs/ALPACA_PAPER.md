# Alpaca paper trading

Runs the three strategies that survived fees in the walk-forward on Alpaca's
**paper** account (simulated money), so they can earn real forward evidence.

| Sleeve | Timeframe | Entry | Exit |
|---|---|---|---|
| `regime_1h` | 1h | market | 3% stop, 25% target, chandelier break or ADX chop |
| `regime_4h_mk` | 4h | limit 0.1% under close, one bar | same as above |
| `meanrev_4h_mk` | 4h | limit 0.1% under close, one bar | 3% stop, 25% target, RSI/EMA reverted |

Coins: BTC/USD, ETH/USD, SOL/USD. Long-only spot (Alpaca crypto has no margin,
shorting or futures). Rules are the backtester's, shared through
`backtest_core.regime_signals` / `meanrev_signals`.

## Setup (once)

1. Create a free account at alpaca.markets and open **Paper Trading**.
2. In the paper dashboard, generate API keys.
3. Add them to `.env` (paper keys only):
   ```
   ALPACA_API_KEY=...
   ALPACA_API_SECRET=...
   ALPACA_PAPER=true
   ALPACA_PAPER_BUDGET_USD=100
   ```
4. Check everything before running:
   ```bash
   python scripts/alpaca_paper_runner.py --check
   ```
   It prints the endpoint (must say `paper-api`), account status, live prices,
   spreads, and whether each trade clears Alpaca's minimum order size. With
   $100 over 9 slots each trade is about $11; if a coin shows `TOO SMALL`,
   raise the budget (e.g. `ALPACA_PAPER_BUDGET_USD=300`).

## Run

```bash
python scripts/alpaca_paper_runner.py              # loop, checks every 60 s
python scripts/alpaca_paper_runner.py --report     # results so far
```

Keep it running (a terminal, `tmux`, or a launchd/systemd service). Only one
copy can run at a time. If it stops, restart it: state lives in
`data/alpaca_paper/state.json` and order ids are deterministic, so a restart
never doubles an order. A restart never chases an old signal either: entries
only fire within 15 minutes of a bar closing.

Files:
- `logs/alpaca_paper.log`: what it did
- `data/alpaca_paper/events.jsonl`: every order and decision
- `data/alpaca_paper/trades.jsonl`: closed trades (the evidence)

## When is a strategy ready for real money?

`--report` marks a sleeve **PASS** at 30+ closed trades with profit factor
≥ 1.10 after fees (the repo's promotion gate). At 4h that's roughly 2–3
months; the 1h sleeve gets there faster. Compare the results against just
holding BTC/ETH/SOL over the same weeks.

## Safety

- The runner refuses to start unless `ALPACA_PAPER=true`, and refuses a live
  gateway in code.
- The gateway never sends live orders unless `ALLOW_LIVE_TRADING=true`,
  `PAPER_TRADING=false`, `DRY_RUN=false` and `LIVE_RISK_ACKNOWLEDGEMENT` are all
  set, whatever the caller asks.
- Daily caps: 6 new entries per day, and new entries stop for the day after a
  3% loss of budget (`MAX_DAILY_LOSS_FRACTION`).
- Stops and targets are checked every cycle (60 s), not resting on Alpaca, so a
  very fast move can fill past the stop. The backtest assumed intrabar fills.
- Alpaca charges fees in the coin you receive, so the runner sells what
  Alpaca actually holds; P&L in `trades.jsonl` uses the configured fee rates.

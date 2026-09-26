# Walk-forward backtest, adaptive learner, and the current paper config

_Run: 2026-09-26. Everything here is PAPER. Live stays locked
(`PAPER_TRADING=true DRY_RUN=true ALLOW_LIVE_TRADING=false`)._

## Reproduce

```bash
.venv/bin/python scripts/fetch_kraken_history.py ohlc --tfs 15 60 240 1440   # ~720 bars each
.venv/bin/python scripts/fetch_kraken_history.py trades --days 90             # slow (~2h), resumable
.venv/bin/python scripts/backtest_walkforward.py --tfs 15 60 240 1440         # → data/walkforward_results.json
.venv/bin/python scripts/backtest_walkforward.py --fee-bps 80 --slip-bps 10 --maker-bps 40 --out /tmp/stress.json
```

## Data

Kraken's public OHLC endpoint only returns ~720 bars per interval (15m ≈ 7.5 d,
1h ≈ 30 d, 4h ≈ 120 d, 1d ≈ 2 y). To get more than a week of 15m data,
`fetch_kraken_history.py trades` pages the public **Trades** endpoint and builds
15m bars itself (then resamples to 1h/4h). 90 days of 15m/1h for BTC, ETH, SOL
and PUMP took ~2 h (≈ 11k requests). The loader stitches trade-built bars with
OHLC bars and only keeps the latest contiguous run.

## Method

* Signal on a closed bar, fill at the **next bar's open**. Long-only spot, one position per symbol.
* Costs: taker **40 bps/side + 5 bps slippage/side** (90 bps round trip). `*_mk` variants use a resting
  limit buy 0.1% under the signal close, valid for one bar (skip if not filled), at maker 25 bps/side;
  take-profit exits are maker, stops and signal exits are taker.
* Intrabar stop/TP; if both hit in one bar the **stop** is assumed first; gaps fill at the open.
* Walk-forward: history (after a 210-bar warm-up) split into 5 segments. For k = 1..4 the best grid
  spec on segment k-1 (in-sample, by net expectancy) is scored on segment k (out-of-sample).
  IS numbers are optimistic by construction; only OOS counts.
* Sizing for $ numbers / drawdown: $500, 1% risk to the stop, 40% notional cap. Drawdown is on closed trades.

Families: `breakout` (20/55-bar high + volume, % stop/TP; the old paper sleeve), `momentum`
(RSI band + EMA200 + ADX 25/20 hysteresis, exit < EMA50), `regime` (ADX-trend + EMA20>EMA50 >
EMA200-filter + N-bar high break; exit on chandelier = 22-bar HH − 3×ATR or ADX → chop; sits
flat in chop), `meanrev` (mirrors `MeanReversionStrategy`: RSI ≤ 38 & below EMA50, exit RSI ≥ 55
or back above EMA50).

## Results (OOS, summed over BTC/ETH/SOL/PUMP, base costs)

Full per-symbol table: `data/walkforward_report.txt`.

| tf | family | OOS trades | win % | net bps/trade | $/trade |
|---|---|---:|---:|---:|---:|
| 15m | breakout (old paper sleeve) | 134 | 42.5 | +5.4 | +0.11 |
| 15m | momentum | 694 | 12.0 | −73.6 | −1.09 |
| 15m | regime | 192 | 18.8 | −78.3 | −1.05 |
| 15m | meanrev_mk | 163 | 28.8 | −58.4 | −0.76 |
| 1h | breakout | 93 | 35.5 | −58.9 | −0.89 |
| 1h | momentum | 301 | 18.9 | −58.9 | −0.99 |
| **1h** | **regime** | **49** | **30.6** | **+71.4** | **+1.26** |
| 1h | regime_mk | 51 | 29.4 | +35.6 | +0.74 |
| 1h | meanrev_mk | 94 | 56.4 | −14.9 | −0.25 |
| 4h | breakout | 46 | 39.1 | −27.6 | −0.51 |
| 4h | momentum | 130 | 27.7 | −74.3 | −1.36 |
| 4h | regime_mk | 24 | 29.2 | +90.6 | +1.42 |
| 4h | meanrev_mk | 27 | 70.4 | +110.5 | +1.76 |
| 1d | all families | — | — | negative | — |

1h regime per symbol (OOS): BTC 10 trades +88 bps, ETH 11 +116, SOL 7 +528, PUMP 21 −112.

Robustness check of the fixed DEFAULT regime@1h spec (no parameter selection), BTC+ETH+SOL,
38 trades: mean +130 bps but **median −98 bps**, win 29%; dropping the best 2 trades → +3 bps.
At stress costs (80 bps/side + 10 slip) mean +41, ex-best −21. Current 15m breakout at stress
costs: −81 bps. meanrev_mk@4h: 23 trades, median +97, ex-best-2 +60 (base) / ≈0 (stress) —
a lead, but ~2 trades/month/symbol.

## Verdict

**No strategy has a demonstrated edge yet.** The 1h regime sleeve is the best by
out-of-sample expectancy after taker fees with a usable sample, and it is positive on BTC, ETH
and SOL individually — but ~50 OOS trades over 90 days, a 30% win rate, and dependence on a
couple of big trend winners mean it could easily be noise. Momentum loses everywhere; the old
15m breakout is fee-breakeven at best. Dublin-repo leads: BTC is *not* negative at every window
here (regime@1h/4h positive); PUMP mean-reversion with limit entries is ≈0 at 1h (+8 bps, 37
trades) and +92 bps at 4h (10 trades) — unconfirmed.

## What runs now (scripts/run_paper_mac.sh)

`STRATEGY=regime_trend TIMEFRAME_MINUTES=60 STOP_LOSS_PCT=0.03 TAKE_PROFIT_PCT=0.25`
(TP is deliberately wide; exits are the chandelier/ADX-chop signal or the 3% hard stop),
BTC/ETH/SOL, max 3 positions, 1% risk, $500 paper book (`PAPER_USE_LEDGER_EQUITY=true`),
paper fill model 40/25 bps + 10 bps slippage.

## Adaptive learner (`learner.gate`)

* Every paper exit is recorded with its cost basis → net bps after fees, tagged with the strategy
  key (`regime@60m`) and the ADX regime at ENTRY (`trend` / `chop` / `volatile_chop`).
* Per symbol and per symbol|regime: rolling expectancy over the last 20 closed trades, blended
  with the walk-forward OOS prior for the same strategy key (prior capped at 5 pseudo-trades).
* ≥ 8 live trades with negative expectancy → **bench** 72 h (entries blocked; exits never gated),
  then one **probation** trade at 0.25× size; still negative → re-bench, positive → cleared.
* Not benched: blended < 0 → 0.5× size; 0–25 bps → 0.75×; else 1×. A prior alone can never bench.
* Legacy learner trades (no cost basis, older strategies — BTC 18/1, PUMP 4/0) are kept for
  reporting but do not count toward the new strategy's gate.
* Decisions are appended to `logs/learner_decisions.jsonl` on change; the paper loop logs a
  `LEARNER` summary at start and a `LEARNER gate` line whenever a BUY is evaluated.

## Stale paper lots (Sep 21 breakout@15m ETH/SOL)

Closed mark-to-market on 2026-09-26 at the public bid via the paper fill model (recorded as
paper exits, credited to `breakout@15m` in the learner): ETH −$0.15 (−236 bps incl. the old
80 bps entry fee), SOL +$0.21 (+83 bps). Old book ($97 budget) ended at $102.44 cash and was
archived to `logs/paper_portfolio.archived-20260926T140436Z.json`; a fresh $500 paper book started.

# TREND-6 independent verification (2026-11-16)

**Verdict: the headline numbers hold.** A fresh re-implementation, written from the spec before reading
`research/edge5/ensemble.py` and run on **different data (Coinbase Exchange)**, reproduces CAGR, Sharpe, max
drawdown, the year-by-year path and the circular-shift p to within about 1 point. Three secondary claims are
wrong or overstated: **profit factor** (≈5.1, not 6-7), **"~13 position changes per coin per year"** (≈130 weight
changes; 13 is the number of on/off stretches) and **Kraken fee dollars** (≈$11/yr at maker on $100, not $18-20).
The luck test is **weaker under stricter nulls** (p 0.008-0.05 depending on the method, 0.17 under a null that
keeps 20-day momentum) and the project-wide multiple-testing count (~548 trials) still applies. No look-ahead.
Research only: nothing started, no flags, keys, orders or runners touched.

## Data
| Source | Use | Coverage |
|---|---|---|
| Coinbase Exchange public candles `api.exchange.coinbase.com/products/{BTC,ETH}-USD/candles` (86400 s, paged) | **primary, independent** | BTC 2016-01, ETH 2016-05 → 2026-11 (2 missing ETH days ffilled) |
| Binance Vision spot 1d BTCUSDT/ETHUSDT (the original study's data, `/workspace/edge2-data/xs1d`) | cross-check | 2018-01 → 2026-10 |
| Binance.US `api.binance.us/api/v3/klines` BTCUSD/ETHUSD 1d | spot check only | 2019-09 → now, **19-month hole Jul-2023→Feb-2025** (USD pairs suspended) |

Note: the original backtest used **Binance global USDT prices with Binance.US fees**. It was not run on Binance.US
prices, which can't cover 2019-2026 because of the hole. Binance global `api.binance.com` is geo-blocked from the box.
Raw data is kept outside the repo at `/workspace/t6data` and rebuilt by `research/edge6/fetch_data.py`.

Signal agreement Coinbase vs Binance: identical target weight on 98.5% (BTC) / 99.0% (ETH) of days.
Binance.US spot check (2025-09-15 → 2026-10, the only stretch with 200 days of warmup after the relist): −3.9%
vs −5.3% on Coinbase/Binance, so venue prices make no material difference.

## Claimed vs verified (2019-01-01 → 2026-10-31, 10 bps round trip, signal at close t, trade next open)
| Metric | Claimed | Verified: Coinbase | Verified: Binance Vision | Holds? |
|---|---|---|---|---|
| CAGR | 57.3% | 56.7% | 57.3% | ✅ |
| Buy-and-hold CAGR (50/50 daily-rebalanced) | 49% | 49.5% | 49.3% | ✅ |
| Sharpe (T6 / B&H) | 1.29 / 0.94 | 1.27 / 0.94 | 1.29 / 0.94 | ✅ |
| Max DD (T6 / B&H) | −42% / −76% | −42.0% / −76.3% | −42.2% / −76.3% | ✅ |
| 2022 (T6 / B&H) | −24% / −65% | −23.7% / −65.2% | −23.9% / −65.1% | ✅ |
| Since 2024 (T6 / B&H) | +86% / +42% | +81% / +42% | +86% / +42% | ✅ (+81% on Coinbase) |
| Up years | 7/8 | 7/8 | 7/8 | ✅ |
| Beats B&H years | 4/8 | 4/8 (2019, 2022, 2025, 2026) | 4/8 | ✅ |
| Trades (on/off stretches) | 198 (108 BTC + 90 ETH) | 197 (106 + 91) | 198 (108 + 90) | ✅ |
| Win rate | ~20% | 20.8% | 20.2% | ✅ |
| Profit factor | 6-7 (6.04 BTC, 7.09 ETH) | **5.1** | **5.2** | ❌ overstated (bug below) |
| Avg exposure | 55% | 55% | 55% | ✅ |
| Circular-shift p | 0.004 | 0.003 | 0.007 | ✅ for that method |
| 80 bps since 2024 | +39% vs +42% ("ties") | +34% (+36% trading only on signal change) | — | ≈ (slightly trails) |
| 160 bps | CAGR 27.5%, since 2024 ≈0% | CAGR 26.1%, since 2024 −5% (−2% on-change) | — | ✅ (flat to slightly negative) |
| Position changes / coin / yr | ~13 | **~130 weight-change days**; ~13.5 on/off stretches | 130-134 | ❌ conflated |
| Turnover | 22-24× equity/yr | **28-29×** (the original code also gives 28.0×) | 28× | ❌ |
| Fees on $100/yr | $2.40-5 Binance.US; $18-20 Kraken maker | ≈$1.40 at 10 bps RT; ≈$11 Kraken maker; ≈$22 Kraken taker | | ❌ minor |

Years at 10 bps (Coinbase): 2019 +55.6, 2020 +252, 2021 +158, 2022 −23.7, 2023 +72.6, 2024 +48.2, 2025 +12.0,
2026 YTD +9.2. B&H: +42, +399, +199, −65, +123, +83, −6.1, −17.4.

## Bugs / discrepancies found in the original (diffed after my run)
1. **Profit factor is inflated.** `hunt.trades_pf` charges each trade `cost × mean(weight)` once, but the weight
   changes about 130 days a year inside a stretch and each change costs money. Charging the real turnover gives
   PF ≈ 5.1. Win rate and trade count are unaffected. It still clears the PF ≥ 1.10 bar easily.
2. **"13 position changes per coin per year" is wrong for runner design.** That's the on/off stretch count. The
   target weight moves in 1/12-equity steps (≈$8 on $100) on about 130 days per coin per year, roughly 2-3 times a
   week. Trade count for the "30 closed trades" gate is still ≈27 a year, so ~13-14 months.
3. **Turnover and fee dollars:** ≈28× a year, not 22-24×. At Kraken maker that's ≈$11/yr on $100 (overstated in
   the report), and at 10 bps RT ≈$1.40.
4. **No look-ahead.** The original holds weight(close t) over close t → close t+1, which on 24/7 data is the same
   as trading at the next open. A deliberately planted look-ahead (weight from close t+1) gives CAGR 278% and
   Sharpe 3.4, so the real numbers are nowhere near a leak signature.
5. **Cost accounting is fine.** The original charges |Δtarget| with no drift; the exact drift-aware version
   (trading back to target daily) and an "only trade on signal change" version give the same results within 0.5 pt.

## Benchmark fairness
- B&H definition in the original: 50/50 **daily-rebalanced**, 100% invested, no cost (CAGR 49.5%). A never-rebalanced
  50/50 is weaker at 45.7%. BTC alone 47.5%, ETH alone 43.8%.
- TREND-6 averages 55% exposure, so the like-for-like comparison is **B&H scaled to 55% + cash**: CAGR 32.3%,
  Sharpe 0.94, max DD −52%. TREND-6 beats it clearly: 56.7%, Sharpe 1.27, DD −42%.
- Alpha regression against B&H (Newey-West, 10 lags): beta 0.50, alpha **+21.7%/yr, t = 2.55** (p ≈ 0.01).
- Cash yield: the original earns 0% on cash. Adding T-bill yield on idle cash lifts CAGR to 58.7% and Sharpe to
  1.31 (55%-B&H with T-bills: 34.0%). Conservative as stated.

## Execution and cost sensitivity (Coinbase, 2019 → Oct 2026)
| Cost RT | Next open: CAGR / Sharpe / DD / since-2024 | Close t | 1-day delay |
|---|---|---|---|
| 0 bps | 59.0 / 1.31 / −41 / +89 | 58.7 / 1.30 | 55.6 / 1.24 / −47 |
| 5 bps | 57.8 / 1.29 / −42 / +85 | 57.5 / 1.29 | 54.5 / 1.23 |
| 10 bps | 56.7 / 1.27 / −42 / +81 | 56.4 / 1.27 | 53.3 / 1.21 / −48 / +63 |
| 20 bps | 54.5 / 1.24 / −43 / +74 | 54.1 / 1.24 | 51.1 / 1.18 |
| 80 bps | 41.6 / 1.03 / −48 / +34 | 41.3 / 1.03 | 38.2 / 0.97 / −54 / +20 |
| 160 bps | 26.1 / 0.76 / −55 / −5 | 25.9 / 0.75 | 22.7 / 0.69 / −61 / −16 |

It's robust to a one-day execution delay (−3 pt CAGR). The venue conclusion stands: Binance.US-level costs (≤20 bps)
are fine, and Kraken taker kills the advantage since 2024.

## Start-date sensitivity (10 bps, CAGR / max DD; B&H in brackets)
| Start | TREND-6 | B&H |
|---|---|---|
| 2018-01-01 (includes the 2018 bear) | 43.2% / −51% | (20.8% / −88%) |
| 2019-01-01 | 56.7% / −42% | (49.5% / −76%) |
| 2020-01-01 | 56.9% / −42% | (50.6% / −76%) |
| 2021-01-01 | 36.6% / −42% | (22.7% / −76%) |
| 2021-11-10 (cycle top) | 13.0% / −40% | (−4.2% / −76%) |
| 2022-01-01 | 19.7% / −31% | (2.0% / −68%) |
| 2023-01-01 | 34.7% / −31% | (35.0% / −60%) **tie** |
| 2024-01-01 | 23.3% / −31% | (13.1% / −60%) |

2018 alone: TREND-6 −29% vs B&H −77%. The 2019 start isn't flattering; a 2018 start makes TREND-6 look *better*
relative to B&H. Starting at the 2023 bull start it only ties.

## Does the ensemble add anything over one rule? (10 bps, 2019 → Oct 2026, 50% per coin when on)
| Rule | CAGR | Sharpe | Max DD | Since 2024 | At 80 bps CAGR |
|---|---|---|---|---|---|
| **TREND-6 ensemble** | 56.7 | 1.27 | −42 | +81 | 41.6 |
| SMA200 only | 44.3 | 1.00 | −62 | +64 | 39.9 |
| SMA100 only | 49.5 | 1.08 | −52 | +77 | 41.4 |
| SMA50 only | 63.8 | 1.30 | −56 | +154 | 52.2 |
| 7-day return only | 44.6 | 1.06 | −47 | +23 | 13.8 |
| 14-day return only | 62.4 | 1.29 | −41 | +101 | 38.1 |
| 28-day return only | 57.9 | 1.20 | −53 | +54 | 41.2 |

The ensemble clearly beats the plain 200-day rule (Sharpe 1.27 vs 1.00, DD −42% vs −62%; from 2018 SMA200 is only
29% CAGR / −65% DD). It does **not** beat its best members: SMA50 and 14-day momentum each match its Sharpe. Its
value is not having to pick the winner in hindsight, plus the shallower drawdown. Picking SMA50 now would be
hindsight selection.

## Luck test redone (Coinbase, 10 bps, 1,000 sims unless noted)
| Method | Null keeps | p |
|---|---|---|
| Circular shift of the weight path (original method), Sharpe | exposure, turnover, signal autocorrelation | **0.003** (Binance data 0.007) |
| Run-order permutation per coin (shuffle the holding runs) | exact exposure and turnover per coin | **< 0.001** (0/1000) |
| Joint iid bootstrap of daily BTC+ETH returns → re-run strategy; stat = Sharpe(T6) − Sharpe(B&H) (500) | vol, BTC-ETH correlation; no serial dependence | **0.008** |
| Stationary block bootstrap, mean block 5 d (1,000 → 500) | + ≤5-day dependence | **0.024** |
| Stationary block bootstrap, mean block 10 d (500) | + ≤10-day dependence | **0.05** |
| Stationary block bootstrap, mean block 20 d (500) | + ≤20-day dependence (partly the edge itself) | 0.17 |
| Newey-West alpha vs B&H | — | t = 2.55 (≈0.01) |

Read: the timing is very unlikely to be random (p ≤ 0.008 under every no-edge null). The longer-block nulls keep
some of the 1-4 week momentum the rule feeds on, so the 20-day p = 0.17 mostly says where the edge comes from
(multi-week persistence and volatility clustering), not that it's absent. A fair single number is **p ≈ 0.01-0.02**,
not 0.004. And TREND-6 is trial #548 in this project; only the deflated-Sharpe adjustment in the original covers
that. The prior academic evidence for crypto time-series momentum (Liu & Tsyvinski) carries a good share of the weight.

## What this means for the paper plan
- The edge claim survives an independent check on independent data. Use the corrected numbers: PF ≈ 5,
  turnover ≈ 28×/yr, rebalances on ≈130 days/coin/yr in ≈$8 steps on a $100 book, ≈27 closed stretches a year.
- A runner must handle frequent small rebalances. Every step is above Binance.US's $1 minimum, but you can't
  assume "13 trades a year".
- Expect to trail buy-and-hold in strong bull years (2020, 2021, 2023, 2024), and that's normal for this rule.

## Files (`research/edge6/`)
- `fetch_data.py`: pulls Coinbase + Binance.US daily candles to `/workspace/t6data` (outside the repo).
- `data.py`: loaders (Coinbase, Binance Vision cache, Binance.US).
- `trend6.py`: independent engine (signals, 4 execution modes incl. a planted look-ahead, 3 cost/rebalance policies,
  benchmarks, trades/PF).
- `run_verify.py`, `run_bootstrap_iid.py`: all tables above. Output in `verify.json`, `log_verify.txt`.

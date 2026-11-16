# Daily trend filter (D1) and the trend-hold@240m paper sleeve

PAPER ONLY. Nothing here changes a safety lock (`PAPER_TRADING`, `DRY_RUN`,
`ALLOW_LIVE_TRADING`). Both features refuse to run unless all locks are engaged.

Source of every number below: `scripts/study_d1_trendhold.py` (pre-registered,
decision rule fixed before the run). Output files: `data/d1_trendhold_report.txt` and
`data/d1_trendhold_results.json`. Reproduce it with:

```bash
python scripts/study_d1_trendhold.py --data-dir <pipeline data> --daily-dir <dir with *_1440.csv>
```

Data: the 121-day tick pipeline (Jun 4 → Oct 2 2026) for BTC/ETH/SOL, plus Kraken
public daily OHLC. The daily closes match the tick-built UTC daily closes to 0.0 bps
over 119 days.

Costs:
- Base: taker 40 bps plus 5 bps slippage per side, maker 25 bps.
- Stress: 80 + 10.

## 1. Daily risk-on filter (D1)

`src/dublin_bot/daily_filter.py`

- **risk-on** means the last *closed* UTC daily close is above SMA50 of daily closes, and
  SMA50 is above its value 5 days earlier.
- **No look-ahead.** An intraday bar only sees a daily bar whose close is at or before
  that intraday bar's close. Today's forming daily bar is never used.
- **Fails closed.** While SMA50 is warming up, or if daily data is unavailable, entries
  are blocked. Live, the daily bars come from Kraken *public* OHLC, cached per UTC day.
  A stale value from the same day is reused if a refresh fails. No private endpoint is
  ever called.
- **Gates entries only.** It never forces an exit and never blocks one. Held positions
  keep their normal exits (ATR trail, regime exit, TP/stop, and so on).
- **Config** (all default `true`):

  | Setting | Applies to |
  |---|---|
  | `REGIME_DAILY_FILTER` | regime_trend@60m |
  | `MEANREV_DAILY_FILTER` | meanrev_mk@240m |
  | `TRENDHOLD_DAILY_FILTER` | trend-hold@240m |

- When D1 blocks an entry, the WAIT reason says so: `D1 risk-off (...)` or
  `D1 unknown ... entries blocked`.

### Walk-forward with and without D1 (net bps per trade, base costs)

**Decision rule (pre-registered).** D1 ships default-OFF only if it *clearly hurts*:
the out-of-sample (OOS) view-A sum is lower **and** D1 is worse in at least 3 of the 4
OOS folds.

**regime@60m**

| View | No D1 | D1 |
|---|---|---|
| A: fixed OOS, folds 1–4 | n=40, mean +112, median −117, ex-best-2 −8, stress +25, sum +4501 | n=29, mean +196, median −128, ex-best-2 +32, stress +108, sum +5680 |
| B: walk-forward select | n=38, mean +107 | n=29, mean +164 |
| Audit split, IS (< Aug 3) | n=26, mean −52 | n=2, mean −130 |
| Audit split, OOS (≥ Aug 3) | n=27, mean +232 | n=27, mean +220 |

Fold means (folds 1 / 2 / 3 / 4):

| Variant | Fold 1 | Fold 2 | Fold 3 | Fold 4 |
|---|---:|---:|---:|---:|
| No D1 | −135 | −89 | +393 | +176 |
| D1 | −130 | −151 | +393 | +176 |

D1 is worse in 1 of 4 folds (fold 2), so it ships **default ON**. Its real effect is to
take the strategy out of the market during the down-trend part of the sample: in the
audit's IS window it cuts trades from 26 to 2. It does almost nothing once the daily
trend is up, as in the audit OOS window.

**meanrev_mk@240m.** OOS is identical with and without D1: 14 trades, 79% win, mean
+100, median +91, ex-best-2 +43, stress +54. In the IS split D1 cuts trades from 12 to
6. Not worse in any fold, so it ships **default ON**.

Caveat: the regime OOS sample is still small (29–40 trades), and the median trade is
negative. Most of the edge comes from a few large trend wins.

## 2. trend-hold@240m (third paper sleeve)

`strategies/trendhold_strategy.py`, `trendhold_sleeve.py`, `backtest_core.trendhold_signals`

- **Entry.** After a 4h bar closes with D1 on, close > EMA100 and EMA20 > EMA100, the
  sleeve does a paper market buy on the next cycle, which is the next bar's open (taker
  fee plus slippage). It never chases a signal more than one bar old.
- **Exit.** At the first 4h close below EMA100, on a bar that closed after the fill,
  it does a paper market sell. No hard stop and no TP, as specified.
- **Size.** 25% of the $500 paper book per coin (`TRENDHOLD_POSITION_FRACTION`, at most
  0.34), times the learner size multiplier. It is capped by the room left under the
  total exposure cap and by free cash.
- **Universe.** BTC/USD, ETH/USD and SOL/USD only. This is fixed in code and does not depend on `UNIVERSE_ALLOWLIST`; `TRENDHOLD_SYMBOLS` can only narrow it.
- **State.** `logs/trendhold_sleeve.json`. The last cycle is written to
  `logs/last_cycle_trendhold.json`. Each cycle writes a `CYCLE sleeve=trendhold_4h ...`
  line to the paper log.

### Cross-sleeve limits (all three sleeves together)

| Limit | Value | How it's enforced |
|---|---|---|
| One position per coin across ALL sleeves | — | `logs/paper_sleeve_owners.json` (`SleeveRegistry`) is extended with the `trendhold_4h` owner. Every sleeve skips a coin another sleeve holds or has pending, and the primary engine already honours the registry. |
| `MAX_CONCURRENT_POSITIONS` | **3** (unchanged) | Counts every held lot plus every pending order from every sleeve. |
| `MAX_POSITION_FRACTION` | **0.25** (unchanged) | |
| `MAX_EXPOSURE_FRACTION` | **0.75** in `run_paper_mac.sh` (was 0.50) | This is the **only limit change**. It lets three 25% positions coexist. Exposure counts every lot at market plus pending orders. The code default stays 0.50 for non-paper use. |
| Daily-loss breaker, drawdown breaker, cooldown | — | Shared `RiskManager` + `SessionState`. |

The shared order cooldown (`COOLDOWN_MINUTES`, default 10) still applies. When several
coins signal on the same 4h close, they enter one after another, roughly one cooldown
apart, within the same 4h bar. They do not all enter at that bar's open as the backtest
assumes.

### Out-of-sample result vs buy-and-hold (Aug 3 → Oct 2 2026, $500 book)

| Variant | Return | Max DD (mark-to-market) | Max DD (realized only) | Trades |
|---|---:|---:|---:|---:|
| 25%/coin + D1 (**as shipped**) | **+16.15%** | −7.36% | −3.58% | 18 |
| 1/3 per coin + D1 (audit's book) | +21.22% | −9.49% | −4.75% | 18 |
| 25%/coin + D1, stress costs | +11.54% | −8.87% | −5.08% | 18 |
| 25%/coin, no D1 | +16.54% | −7.40% | −3.81% | 19 |
| **Buy & hold, equal weight** | **+43.66%** | −7.41% | — | — |

- **Buy & hold by coin:** BTC +32.8%, ETH +41.7%, SOL +60.5%.
- **Per-trade results (25% + D1):** 33% win, mean +367 bps, median −179 bps.
- **Per coin:**

  | Coin | Trades | Mean bps | Median bps |
  |---|---:|---:|---:|
  | BTC | 6 | +215 | −166 |
  | ETH | 6 | +394 | −163 |
  | SOL | 6 | +492 | −212 |

- **IS check (Jul 9 → Aug 3):** −1.31% with D1 (3 trades) and −3.47% without, against
  buy & hold +0.65%.

**Audit claim check.** The audit claimed +20% and −4.7% max drawdown. The return
reproduces within about a point on the audit's 1/3-per-coin book (+21.2%). The −4.7% is
a **realized-only** drawdown: it is computed on closed-trade equity, and our
realized-only figure on the same book is −4.75%. The honest mark-to-market drawdown of
that book is −9.5%, and −7.4% at the shipped 25% size.

**In a strong up-trend, trend-hold lagged buy-and-hold by about 27 points**: +16% vs
+44%, with about the same mark-to-market drawdown. It is a paper experiment in risk
reduction (time out of the market in down-trends), not a proven edge.

## Promotion

Neither feature can promote anything to live. The live-promotion bar (at least 30
closed paper trades per strategy, a positive 90% lower bound, and so on) is a separate
report-only check.

# Edge search: signals with a structural cause, 2026-11-14

Research and paper analysis only. All data is public: data.binance.vision spot 15m klines, USDT-M funding history and 1h premium-index klines, plus the OKX public funding history for Nov 2026. No keys were used, everything ran under `env -i`, and nothing was restarted, wired up or ordered. Ledger's folder was read only.
Code: `research/edge2/`. Raw data cache: `/workspace/edge2-data` (outside the repo). Rerun with `fetch.py`, `load.py`, `idea1..5_*.py`, `wf_all.py`, then `candidate.py dev|holdout`.

## Setup
- **Universe:** 12 liquid USDT spot pairs (BTC ETH SOL XRP DOGE ADA AVAX LINK LTC BNB DOT TRX), 15m bars resampled to 1h, 2023-01-01 to 2026-11-13. Binance global prices stand in for Binance.US.
- **Dev:** 2023-01-01 to 2026-09-30. **Holdout:** 2026-10-01 to 2026-11-13, untouched until the final candidates were frozen and evaluated **once**.
- **Execution:** the signal is computed on a closed 1h bar and filled at the next 1h open. Spot is long-only. Time exits unless stated otherwise.
- **Round-trip costs:**
  - Binance.US base: 10 bps (2 bps taker + 3 bps slip per side).
  - Binance.US stress: 14 bps.
  - Kraken maker best case: 80 bps (40/side).
  - Kraken taker: 170 bps (80+5/side).
- **Purged walk-forward:**
  - Quarterly test folds, with a train window of the prior 4 quarters and a 72h embargo (train trades must exit 72h before the test starts).
  - The best train variant within the family is selected and applied; the strategy sits out if no variant was positive.
  - That gives 11 OOS folds (2024Q1 to 2026Q3). Folds with no trades count as **not** positive.
- **Gross edge:** mean forward return after the signal minus the unconditional (random-entry) mean over the same hold. Because the 12 coins dump together, I also cluster by event day.
- **Multiple testing:** **376 variants** were evaluated (F1 72, F2 108, F3 24, F4 44, F5 128), plus 2 frozen portfolio configs, for **378 in total**. The deflated Sharpe (Bailey/López de Prado) uses day-clustered returns, N=376 and the cross-variant SR variance.

## Results, one line per idea (walk-forward OOS, dev period)
| # | Idea | Gross edge? | Binance.US (10 bps RT) | Kraken maker (80 bps RT) | DSR |
|---|---|---|---|---|---|
| 1 | Funding / basis extremes (12 coins) | **Weak.** The low-funding/basis tail is +12 to +49 bps vs random, but it is concentrated in bull quarters, the high tail is pure momentum beta, and the WF flips between them | 741 tr, PF 1.62, **4/11 folds**, 2026 −17 bps/tr | 714 tr, PF 1.20, 3/11 folds | 0.00 |
| 2 | Post-capitulation bounce (24h z≤−2.5 + 2× volume) | **Yes**, broad plateau: +100 to +300 bps excess at 24-48h, day-clustered t≈3 | 73 tr, PF 5.77, 6/11 folds (9 traded) | 73 tr, PF 4.70, 6/11 | 0.19 / 0.09 |
| 3 | Cross-sectional RS rotation | **No.** Top-k vs basket \|t\|<1.6; the only "signal" is laggards continuing to lag | 429 tr, PF 0.98, 4/11 | 324 tr, PF 0.83, 4/11 | 0.00 |
| 4 | Time-of-day / day-of-week | **No.** Hour-of-day signs flip between dev halves. Wed/Fri +25-31 bps, but that is not robust | 1,728 tr, PF 0.77, 5/11 | 780 tr, PF 0.61, 0/11 | 0.00 |
| 5 | Ledger DualThrust re-verify (SOL/ETH, 64-spec grid each) | Fixed-spec dev looks great (SOL rode a 20× bull), but the **WF fails** | 106 tr, PF 0.86, 2/11 (SOL-only 95 tr, PF 1.24, 3/11) | PF 0.41 (SOL-only 0.75) | 0.00 |

The DualThrust frozen paper spec (SOL N12 k0.8/0.8):
- **Dev, fixed:** 136 trades, PF 2.24 at Binance.US with 10/15 folds; Kraken maker PF 1.14 with 5/15. This is in-sample parameter choice.
- **Holdout: 5 trades, 0 wins, −65 bps/trade.** This matches Ledger's own 0/8 holdout and its losing forward trade.
- **Verdict:** a multiple-testing artifact. Do not run it.

## Best candidate: CAPIT-24 (post-capitulation bounce), frozen before the holdout
**Rule** (1h bars, all 12 coins):
- z24 = 24h log return / (trailing-30d 1h σ·√24) ≤ −2.5, **and** 24h quote volume ≥ 2× its trailing-30d average.
- Buy at the next 1h open and sell 48h later.
- At most 2 concurrent positions, one per coin, each 50% of equity. When signals tie, take the most negative z first.
- The `+liq` version adds a "perp basis in the bottom 10% of the trailing 90d" filter (a liquidation proxy). It needs a futures data feed.

**Dev results (portfolio-constrained):**

| | CAPIT-24 | CAPIT-24+liq |
|---|---|---|
| Trades | 125 | 41 |
| Binance.US base: PF / mean / win | 1.63 / +124 bps / 56% | 2.94 / +314 bps / 71% |
| Binance.US stress PF | 1.60 | 2.90 |
| Kraken maker PF | 1.24 (9/15 folds) | 2.32 (9/12) |
| Kraken taker PF | 0.87 (fail) | 1.70 |
| Positive quarters (Binance.US) | 10/15 | 9/15 (3 empty) |
| Return / max DD (50% slots, non-compounded) | +77% / 38% | +64% / 18% |
| Net, ex the best 3 / best 5 days | 15,489 → 7,758 → 3,778 bps | 12,855 → 4,988 → 1,839 bps |
| **2026 Jan-Sep (BTC bear)** | **23 tr, −213 bps/tr** | **11 tr, −252 bps/tr** |
| By year (bps/tr, Binance.US) | 2023 −15, 2024 +320, 2025 +303, 2026 −213 | 2023 +655, 2024 +635, 2025 +359, 2026 −252 |
| Deflated Sharpe (N=376, day-clustered) | **0.00** | **0.25** |
| Worst trade / worst MAE | −12.8% / −59.5% (LINK, Oct 10 2025 flash crash) | −24.1% / −24.7% |

**Holdout (Oct 1 to Nov 13, evaluated once):**
- CAPIT-24: 2 trades, both winners. BNB +0.9% and LINK +11.1% gross, from the Oct 29-30 dump. Net +590 bps/tr at Binance.US and +520 at Kraken maker.
- CAPIT-24+liq: 2 trades. BNB −0.5% and ETH +1.8%. Net +52 bps/tr.
- The holdout is non-negative, but **2 trades say nothing statistically.**

**Against the promotion bar:**
- ≥30 trades: ✅
- Majority of folds positive: ✅ (10/15)
- PF ≥1.10 after fees: ✅ at Binance.US, ✅ at Kraken maker, ❌ at Kraken taker
- Holdout non-negative: ✅
- **Sane deflated Sharpe: ❌** (0.00; the +liq version is 0.25)

**It does NOT pass.**

## Honest read
- **Price patterns:** confirmed again. Neither cross-sectional RS nor seasonality has a gross edge, and DualThrust does not survive walk-forward or the holdout.
- **Funding/basis:** some of the gross tilt is real, but it is not stable enough to trade.
- **Post-capitulation bounce:** the only idea with a real, broad gross effect, with many neighbouring parameter sets positive. The cause is plausible: forced selling, liquidations and overshoot.
- **But it is regime-dependent.** It made its money in the 2024-25 bull, lost in the 2026 bear (−213 bps/tr), and about half its profit comes from 3-5 crash days. After counting 376 variants, the deflated Sharpe gives no statistical confidence.
- **Kraken fees:** they kill everything except the rare, large-move setups. Binance.US fees are a precondition.
- **Recommendation:** nothing is ready to promote and nothing should be wired in. If the user wants to keep going, the one thing worth doing is a **shadow forward test** of CAPIT-24, which logs signals only and places no orders. Kill it if, after 30 forward signals, the net mean is below 0 or PF is below 1.10. It fires about 30-35 times a year, so that takes about a year. The plan is below.

## Shadow forward-test plan for CAPIT-24 (not started; needs the user's OK)
- **Mode:** shadow/paper only. PAPER_TRADING, DRY_RUN and the live flags stay as they are. It is not wired into the mayo-bot loop.
- **Data:** Binance.US public 1h klines for the 12 coins (check each is listed on Binance.US). There is no futures feed, so run the plain CAPIT-24 rather than `+liq`.
- **Logic:**
  - Hourly, at bar close: compute z24 and the 24h volume ratio.
  - On a signal, record a virtual buy at the next open and a virtual sell 48h later.
  - Max 2 open, 50% of equity each.
  - No stop (the backtest used none). Note the flash-crash MAE risk; a −20% catastrophe stop is the only optional addition.
- **Logging:** each signal, its fill price and the 48h result, at both Binance.US (10 bps) and Kraken (80 bps) costs.
- **Promotion review at ≥30 forward trades:** PF ≥1.10 after Binance.US costs, mean > 0, no worse than −15% drawdown at 50% slots, and it beats buy-and-hold of the basket over the same period.
- **Kill early:** if the first 15 forward trades have a net mean below −100 bps.

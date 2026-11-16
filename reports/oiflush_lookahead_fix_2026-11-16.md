# OI-flush look-ahead re-test — 2026-11-16

**Research only. Nothing was wired. The paper runner and the shadow loop were not touched.**

Code: `research/edge3/oi_lookahead_fix.py` (results `oi_lookahead_fix.json`, log `log_oi_fix.txt`),
comparator `research/edge3/oi_lookahead_capit_only.py` (`oi_lookahead_capit_only.json`).

## The worry

Round 2 (`research/edge3/c1_oi_liq.py`, `wf_r2.py`) built hourly OI as "last Binance USDT-M metrics
print with `create_time` in [H, H+1h)" and treated it as known at the close of bar H. The print that
reflects OI at H+1h:00 is stamped about H+0h:55. So the signal could be peeking at a snapshot that
is not actually published until the entry bar opens, or later.

## What was re-run

The **exact** same family, `OI|capit+oiflush|q{0.10,0.25}|h{12,24,48}` on the same 12 coins (BTC ETH
SOL XRP DOGE ADA AVAX LINK LTC BNB DOT TRX). It used the same CAPIT mask, the same trailing-quantile
`rq`, the same `simulate`, and the same purged walk-forward (4-quarter train, 72h embargo, 11 test
quarters 2024Q1–2026Q3). The deflated Sharpe uses the same cumulative N = 520 variants and the same
SR variance as `wf_r2.py`. The permutation test is the `gates.py` one (1,000 random-entry runs, same
coins and holds). With 1,000 runs, **p = 0.001 is the floor**.

Three OI timing assumptions:

| Mode | Hourly OI value at bar H |
|---|---|
| `orig` | last print with create_time in [H, H+1h): the published result |
| `p5m` | create_time **+5 min**, then the same bucketing (raw stamp in [H−5m, H+55m)) |
| `p1h` | last print with create_time in [H−1h, H): **conservative**, fully known before bar H even opens |

Sanity check: `orig` reproduces round 2's `metrics_1h.pkl` exactly (max abs diff 0.0), and its
walk-forward reproduces the original 188 trades / PF 3.72 / 8 of 11.

## Results — walk-forward (out-of-sample picks), the number that was quoted

| OI timing | Fee | Trades | PF | Win % | Folds + | Perm p | DSR | 2026 trades / mean net bps |
|---|---|---|---|---|---|---|---|---|
| **orig** (published) | 10 bps | 188 | 3.72 | 70.7 | 8/11 | 0.001 | 0.148 | 53 / +83.9 |
| p5m | 10 bps | 190 | 3.97 | 71.6 | 9/11 | 0.001 | 0.159 | 53 / +82.4 |
| p1h | 10 bps | 185 | 4.10 | 73.5 | 9/11 | 0.001 | 0.189 | 52 / +97.8 |
| **orig** (published) | 80 bps | 188 | 2.86 | 67.6 | 8/11 | 0.001 | 0.027 | 53 / +13.9 |
| p5m | 80 bps | 190 | 3.04 | 68.4 | 8/11 | 0.001 | 0.029 | 53 / +12.4 |
| p1h | 80 bps | 185 | 3.14 | 69.2 | 8/11 | 0.001 | 0.042 | 52 / +27.8 |

## Results — the frozen shadow variant (q=0.10, hold 48h), full dev window 2023-02 → 2026-09

| OI timing | Fee | Trades | PF | Win % | Quarters + | Perm p | DSR |
|---|---|---|---|---|---|---|---|
| orig | 10 bps | 232 | 3.87 | 69.4 | 12/15 | 0.001 | 0.034 |
| p5m | 10 bps | 234 | 4.03 | 70.1 | 13/15 | 0.001 | 0.040 |
| p1h | 10 bps | 226 | 4.18 | 72.1 | 12/15 | 0.001 | 0.101 |
| orig | 80 bps | 232 | 2.96 | 64.7 | 11/15 | 0.001 | 0.003 |
| p5m | 80 bps | 234 | 3.08 | 65.8 | 11/15 | 0.001 | 0.003 |
| p1h | 80 bps | 226 | 3.20 | 67.3 | 12/15 | 0.001 | 0.013 |

(Not frozen pre-test, so in-sample. Shown only because it is the rule the shadow loop runs.)

## Comparator — same CAPIT rule, NO OI filter, same 12 coins, same harness

| | Fee | Trades | PF | Win % | Folds + | Perm p | DSR |
|---|---|---|---|---|---|---|---|
| Walk-forward (holds 12/24/48) | 10 bps | 204 | 3.57 | 70.1 | 8/11 | 0.001 | 0.087 |
| Walk-forward | 80 bps | 204 | 2.72 | 65.7 | 8/11 | 0.001 | 0.011 |
| Frozen h48, dev | 10 bps | 258 | 3.43 | 69.0 | 11/15 | — | — |
| Frozen h48, dev | 80 bps | 258 | 2.62 | 62.8 | 11/15 | — | — |

## Verdict (blunt)

1. **The look-ahead was not the source of the result.** Shifting OI +5 min, or a full hour back,
   does not hurt it. The numbers get slightly *better* (PF 3.72 → 3.97 → 4.10 at 10 bps). The
   look-ahead concern is closed.
2. **On paper it clears the promotion bar** at both 10 bps and 80 bps under the conservative +1h
   timing: 185 trades (≥30), 9/11 or 8/11 folds positive, PF 4.10 / 3.14 (≥1.10), and permutation
   p = 0.001 (<0.01).
3. **But "OI-flush" is mostly just CAPIT.** The OI filter keeps about 88% of CAPIT's trades (226 of
   258) and lifts PF from 3.43 to about 3.9–4.2. That is a modest filter, not a new edge. On a
   crash day, OI almost always flushes too.
4. **It fails the deflated-Sharpe gate badly.** DSR is 0.19 at best at 10 bps and 0.04 at 80 bps,
   against a 0.95 gate. After 520 variants were tried, this level of Sharpe is not distinguishable
   from the best of many lucky tries once you count that many trades land on the same market-wide
   crash days. The permutation test does not catch this, because it randomizes entry times
   but not the selection over 520 variants.
5. **2026 is weak at Kraken fees**: +12 to +28 bps mean net per trade at 80 bps, versus about
   +280 bps over the whole window. The edge has thinned recently.
6. **It does not apply to the paper book's coins.** This test covers 12 large caps with Binance
   USDT-M futures. VET, HBAR, ALGO and XLM, the paper book, were not tested with OI. The
   live shadow can only run OI-flush on coins with Binance futures metrics, and the OI data
   arrives about a day late (daily vision files).

**Recommendation:** leave the paper runner and the shadow loop exactly as they are. The `oiflush`
shadow variant is still worth keeping as a forward test because its timing is now known to be
clean. Do not promote it over base CAPIT-24: the forward evidence is zero trades, the DSR gate
fails, and the OI filter adds little on top of CAPIT. Revisit after the shadow has 30 or more closed
`oiflush` trades.

**Note on the live shadow:** `research/edge2/capit_shadow.py` builds hourly OI with the same `orig`
bucketing (`resample('1h', label='left', closed='left').last()` on raw create_time). Given the result
above this does not matter much. If we ever freeze a v2 of the shadow, switch it to the `p1h`
convention so the forward test is clean by construction. That is a recommendation only; the loop was
**not** changed.

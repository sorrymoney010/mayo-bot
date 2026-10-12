# Signal-frequency / gate-blocker study (2026-10-12, paper research)

Data: 1m bars data/bars1m (2026-07-11..10-11) resampled, prepended with public Kraken 60m/240m caches (from 2026-06-04);
D1 = public daily + 1m-built days. Alts: public Kraken OHLC 60m (~30d) / 240m (~120d) / 1440m, no keys.
4h 120d window effectively covers ~94d (meanrev, 210-bar warmup) / 112d (trendhold).
Near-signal = exactly one entry condition fails (meanrev RSI miss must be within 7 pts).
Events = rising edges (episodes), not bars. Fee min-edge gate never binds (TP 25% >> 100 bps).

| strategy | window | BTC full/near | ETH full/near | SOL full/near | near-miss blockers (sum BTC+ETH+SOL) |
|---|---|---|---|---|---|
| regime_trend@60m | 30d | 10/27 | 7/21 | 11/27 | breakout 47, ADX 29, ema20>50 1, D1 0, fee 0 |
| regime_trend@60m | 120d | 20/85 | 24/84 | 27/77 | breakout 120, ADX 57, D1 57, ema20>50 17, ema200 1, fee 0 |
| meanrev_4h | 30d | 4/13 | 3/8 | 3/10 | RSI 29, D1 2, fee 0 |
| meanrev_4h | 120d(~94d) | 12/37 | 10/26 | 8/28 | RSI 72, D1 19, fee 0 |
| trendhold_4h | 30d | 2/6 | 2/4 | 5/6 | close>EMA100 10, EMA20>100 6, D1 0, fee 0 |
| trendhold_4h | 120d(~112d) | 7/23 | 8/17 | 8/17 | close>EMA100 22, D1 22, EMA20>100 14, fee 0 |

Variants (228 on majors, 228 on alts; 60m and 240m; fees 40/25/10 bps per side + 5 bps slip on taker).
Holdout = trades entered in the last 60 days. Bar: n>=30, mean>0, one-sided 95% LCB>0, mean ex best 2 >0.

* Live configs: none pass (regime@60m n=28, LCB -30 bps @40; meanrev n=14; trendhold n=12, strong but too few).
* Majors: only passes are at 10 bps (regime@60m lookback 10 / ADX 18-14); none at 25 or 40 bps.
* Alts (XRP DOGE ADA AVAX LINK LTC DOT SUI pooled): regime@240m, D1 OFF, passes at 40/25/10:
  ADX25/20 lb20 @40bps n=39 mean +356 bps LCB +60 ex-best2 +246; also lb10 and ADX22/18 at 40 bps.
  With D1 ON the same config fails (LCB -123). Per coin n=3-7, so no single coin passes alone; LINK negative.
  The same config on BTC/ETH/SOL @240m fails. Selected from a 76x3 grid: multiple-testing risk is real.

# Order-flow filters + exit timers — 2026-10-11T20:02:38Z

Paper research only. Ticks read-only; no lock, setting or live process touched.
Costs: taker 40 + slip 10 bps/side, maker 25; stress 80+10. Configs tried: 35 (pre-registered, fixed thresholds, one run).

- data BTC/USD: longest contiguous tick run 2026-06-04 21:00 .. 2026-10-04 14:00 UTC (121.7 d)
- data ETH/USD: longest contiguous tick run 2026-06-04 21:00 .. 2026-10-04 14:00 UTC (121.7 d)
- data SOL/USD: longest contiguous tick run 2026-06-04 21:00 .. 2026-10-04 14:00 UTC (121.7 d)

## regime@60m  `regime(adx_enter=25.0,adx_exit=20.0,atr_mult=3.0,d1=True,lookback=20,min_atr_rank=0.0,stop=0.03,tp=0.25)`
OOS days per coin: {'BTC/USD': np.float64(90.3), 'ETH/USD': np.float64(90.3), 'SOL/USD': np.float64(90.3)}; buy-and-hold over the OOS window: mean 44.39% (per coin [33.51, 50.44, 49.22]), mean max DD 10.37%
backtest_core cross-check (baseline OOS n/mean per coin): BTC 9/87, ETH 12/92, SOL 9/382

| variant | n | mean bps | median | win% | lb90 | ex-best2 | OOS ret% | maxDD% | stress mean | folds>base | bar | improves | candidate |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 30 | 178 | -136 | 33 | 1 | 18 | 18.38 | 5.59 | 100 | - | PASS | - | - |
| of_no_dist | 29 | 189 | -134 | 34 | 6 | 24 | 18.94 | 5.59 | 111 | 1 | fail | no | - |
| of_no_bear_abs | 30 | 178 | -136 | 33 | 1 | 18 | 18.38 | 5.59 | 100 | 0 | PASS | no | - |
| of_abs_pref | 0 | - | - | - | - | - | 0.00 | 0.00 | - | 0 | fail | no | - |
| of_not_extended | 27 | 80 | -134 | 37 | -31 | -12 | 7.21 | 3.56 | 0 | 1 | fail | no | - |
| x_climax_all | 34 | 78 | -136 | 38 | -34 | -20 | 8.74 | 8.15 | -2 | 2 | fail | no | - |
| x_climax_half | 30 | 130 | -136 | 37 | -13 | 7 | 13.18 | 5.68 | 51 | 2 | fail | no | - |
| x_euphoria_all | 41 | 25 | -62 | 46 | -29 | -2 | 3.31 | 5.49 | -55 | 1 | fail | no | - |
| x_euphoria_half | 30 | 76 | -136 | 33 | -40 | -22 | 7.53 | 5.81 | -2 | 0 | fail | no | - |
| x_shakeout_hold | 30 | 180 | -136 | 33 | 4 | 21 | 18.63 | 5.40 | 102 | 1 | PASS | no | - |
| x_closestop_ctrl | 30 | 179 | -136 | 33 | 3 | 20 | 18.53 | 5.47 | 101 | 1 | PASS | no | - |
| ptp_2atr | 30 | 56 | -75 | 33 | -41 | -26 | 5.52 | 5.71 | -10 | 1 | fail | no | - |
| ptp_4atr | 30 | 61 | -136 | 37 | -43 | -25 | 5.95 | 5.56 | -10 | 2 | fail | no | - |

Walk-forward selection over this sleeve's variants (IS fold k-1 -> OOS fold k): n=30 mean=171 median=-139 chosen=['baseline', 'baseline', 'of_no_dist', 'of_not_extended']

## meanrev_mk@240m  `meanrev(d1=True,entry=limit,limit_offset=0.001,rsi_exit=55.0,rsi_os=38.0,stop=0.03,tp=0.25)`
OOS days per coin: {'BTC/USD': np.float64(69.2), 'ETH/USD': np.float64(69.2), 'SOL/USD': np.float64(69.2)}; buy-and-hold over the OOS window: mean 41.31% (per coin [29.48, 37.24, 57.2]), mean max DD 8.52%
backtest_core cross-check (baseline OOS n/mean per coin): BTC 6/12, ETH 4/282, SOL 4/30

| variant | n | mean bps | median | win% | lb90 | ex-best2 | OOS ret% | maxDD% | stress mean | folds>base | bar | improves | candidate |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 14 | 95 | 86 | 79 | 17 | 38 | 4.47 | 1.24 | 54 | - | fail | - | - |
| of_no_dist | 14 | 113 | 143 | 79 | 36 | 62 | 5.36 | 1.24 | 72 | 2 | fail | no | - |
| of_no_bear_abs | 14 | 95 | 86 | 79 | 17 | 38 | 4.47 | 1.24 | 54 | 0 | fail | no | - |
| of_abs_pref | 6 | 164 | 126 | 83 | 34 | 50 | 3.32 | 0.48 | 124 | 1 | fail | no | - |
| of_not_extended | 14 | 95 | 86 | 79 | 17 | 38 | 4.47 | 1.24 | 54 | 0 | fail | no | - |
| x_climax_all | 15 | 101 | 90 | 87 | 33 | 59 | 5.12 | 1.24 | 60 | 1 | fail | no | - |
| x_climax_half | 14 | 98 | 86 | 86 | 24 | 48 | 4.66 | 1.24 | 58 | 1 | fail | no | - |
| x_euphoria_all | 14 | 113 | 86 | 79 | 28 | 53 | 5.38 | 1.24 | 73 | 2 | fail | no | - |
| x_euphoria_half | 14 | 104 | 86 | 79 | 23 | 48 | 4.93 | 1.24 | 63 | 2 | fail | no | - |
| x_shakeout_hold | 14 | 124 | 86 | 86 | 62 | 72 | 5.90 | 0.48 | 83 | 1 | fail | no | - |
| x_closestop_ctrl | 14 | 124 | 86 | 86 | 62 | 72 | 5.90 | 0.48 | 83 | 1 | fail | no | - |
| ptp_2atr | 14 | 114 | 86 | 86 | 60 | 76 | 5.44 | 0.48 | 84 | 2 | fail | no | - |
| ptp_4atr | 14 | 95 | 86 | 79 | 17 | 38 | 4.47 | 1.24 | 54 | 0 | fail | no | - |

Walk-forward selection over this sleeve's variants (IS fold k-1 -> OOS fold k): n=14 mean=95 median=86 chosen=['baseline', 'x_shakeout_hold', 'baseline', 'of_abs_pref']

## trendhold@240m  `trendhold(d1=True,ema_fast=20,ema_slow=100)`
OOS days per coin: {'BTC/USD': np.float64(69.2), 'ETH/USD': np.float64(69.2), 'SOL/USD': np.float64(69.2)}; buy-and-hold over the OOS window: mean 41.31% (per coin [29.48, 37.24, 57.2]), mean max DD 8.52%
backtest_core cross-check (baseline OOS n/mean per coin): BTC 6/226, ETH 7/332, SOL 6/531

| variant | n | mean bps | median | win% | lb90 | ex-best2 | OOS ret% | maxDD% | stress mean | folds>base | bar | improves | candidate |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 19 | 363 | -181 | 32 | 53 | 90 | 2.78 | 0.65 | 280 | - | fail | - | - |
| of_no_dist | 19 | 354 | -195 | 32 | 42 | 80 | 2.71 | 0.68 | 271 | 0 | fail | no | - |
| of_no_bear_abs | 19 | 362 | -181 | 32 | 52 | 90 | 2.78 | 0.65 | 280 | 0 | fail | no | - |
| of_abs_pref | 13 | -113 | -199 | 23 | -203 | -191 | -0.59 | 0.62 | -192 | 0 | fail | no | - |
| of_not_extended | 19 | 363 | -181 | 32 | 53 | 90 | 2.78 | 0.65 | 280 | 0 | fail | no | - |
| x_climax_all | 35 | 147 | -62 | 49 | -0 | 31 | 2.06 | 1.11 | 66 | 0 | fail | no | - |
| x_climax_half | 19 | 248 | -181 | 32 | -0 | 27 | 1.89 | 0.65 | 166 | 0 | fail | no | - |
| x_euphoria_all | 38 | 78 | 99 | 55 | -10 | 30 | 1.19 | 1.22 | -2 | 0 | fail | no | - |
| x_euphoria_half | 19 | 200 | -165 | 32 | -5 | 28 | 1.53 | 0.65 | 119 | 1 | fail | no | - |
| x_shakeout_hold | 19 | 356 | -181 | 32 | 50 | 90 | 2.72 | 0.65 | 273 | 0 | fail | no | - |
| ptp_2atr | 19 | 158 | -107 | 37 | -15 | 14 | 1.21 | 0.52 | 87 | 2 | fail | no | - |
| ptp_4atr | 19 | 165 | -181 | 32 | -25 | 13 | 1.26 | 0.65 | 91 | 0 | fail | no | - |

Walk-forward selection over this sleeve's variants (IS fold k-1 -> OOS fold k): n=19 mean=130 median=-181 chosen=['baseline', 'ptp_2atr', 'baseline', 'ptp_2atr']

Shadow candidates (bar AND improvement gate): none

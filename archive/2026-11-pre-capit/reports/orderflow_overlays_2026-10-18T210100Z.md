# Order-flow filters + exit timers — 2026-10-18T21:00:59Z

Paper research only. Ticks read-only; no lock, setting or live process touched.
Costs: taker 40 + slip 10 bps/side, maker 25; stress 80+10. Configs tried: 35 (pre-registered, fixed thresholds, one run).

- data BTC/USD: longest contiguous tick run 2026-06-04 21:00 .. 2026-10-18 21:00 UTC (136.0 d)
- data ETH/USD: longest contiguous tick run 2026-06-04 21:00 .. 2026-10-18 21:00 UTC (136.0 d)
- data SOL/USD: longest contiguous tick run 2026-06-04 21:00 .. 2026-10-18 21:00 UTC (136.0 d)

## regime@60m  `regime(adx_enter=25.0,adx_exit=20.0,atr_mult=3.0,d1=True,lookback=20,min_atr_rank=0.0,stop=0.03,tp=0.25)`
OOS days per coin: {'BTC/USD': np.float64(101.8), 'ETH/USD': np.float64(101.8), 'SOL/USD': np.float64(101.8)}; buy-and-hold over the OOS window: mean 30.58% (per coin [28.99, 33.86, 28.89]), mean max DD 14.42%
backtest_core cross-check (baseline OOS n/mean per coin): BTC 10/65, ETH 13/69, SOL 9/370

| variant | n | mean bps | median | win% | lb90 | ex-best2 | OOS ret% | maxDD% | stress mean | folds>base | bar | improves | candidate |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 32 | 153 | -139 | 31 | -14 | 2 | 16.63 | 5.59 | 75 | - | fail | - | - |
| of_no_dist | 31 | 162 | -138 | 32 | -10 | 7 | 17.18 | 5.59 | 84 | 1 | fail | no | - |
| of_no_bear_abs | 32 | 153 | -139 | 31 | -14 | 2 | 16.63 | 5.59 | 75 | 0 | fail | no | - |
| of_abs_pref | 0 | - | - | - | - | - | 0.00 | 0.00 | - | 0 | fail | no | - |
| of_not_extended | 29 | 60 | -138 | 34 | -45 | -28 | 5.62 | 3.56 | -21 | 1 | fail | no | - |
| x_climax_all | 36 | 61 | -139 | 36 | -45 | -31 | 7.14 | 8.15 | -19 | 2 | fail | no | - |
| x_climax_half | 32 | 108 | -139 | 34 | -28 | -8 | 11.50 | 5.68 | 29 | 2 | fail | no | - |
| x_euphoria_all | 43 | 14 | -75 | 44 | -39 | -13 | 1.79 | 5.49 | -66 | 0 | fail | no | - |
| x_euphoria_half | 32 | 58 | -139 | 31 | -52 | -35 | 5.94 | 5.81 | -21 | 0 | fail | no | - |
| x_shakeout_hold | 32 | 154 | -139 | 31 | -12 | 4 | 16.87 | 5.40 | 77 | 1 | fail | no | - |
| x_closestop_ctrl | 32 | 154 | -139 | 31 | -12 | 4 | 16.78 | 5.47 | 76 | 1 | fail | no | - |
| ptp_2atr | 32 | 41 | -84 | 31 | -51 | -38 | 4.15 | 5.71 | -26 | 2 | fail | no | - |
| ptp_4atr | 32 | 46 | -139 | 34 | -53 | -35 | 4.65 | 5.56 | -25 | 2 | fail | no | - |

Walk-forward selection over this sleeve's variants (IS fold k-1 -> OOS fold k): n=32 mean=153 median=-139 chosen=['baseline', 'baseline', 'of_no_dist', 'of_not_extended']

## meanrev_mk@240m  `meanrev(d1=True,entry=limit,limit_offset=0.001,rsi_exit=55.0,rsi_os=38.0,stop=0.03,tp=0.25)`
OOS days per coin: {'BTC/USD': np.float64(80.5), 'ETH/USD': np.float64(80.5), 'SOL/USD': np.float64(80.5)}; buy-and-hold over the OOS window: mean 27.17% (per coin [24.81, 21.2, 35.49]), mean max DD 14.07%
backtest_core cross-check (baseline OOS n/mean per coin): BTC 9/-16, ETH 6/202, SOL 5/-50

| variant | n | mean bps | median | win% | lb90 | ex-best2 | OOS ret% | maxDD% | stress mean | folds>base | bar | improves | candidate |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 20 | 42 | 86 | 65 | -39 | -7 | 2.73 | 3.68 | 1 | - | fail | - | - |
| of_no_dist | 20 | 10 | 64 | 60 | -71 | -36 | 0.56 | 6.06 | -31 | 2 | fail | no | - |
| of_no_bear_abs | 20 | 42 | 86 | 65 | -39 | -7 | 2.73 | 3.68 | 1 | 0 | fail | no | - |
| of_abs_pref | 12 | -11 | 106 | 58 | -131 | -92 | -0.49 | 4.88 | -51 | 2 | fail | no | - |
| of_not_extended | 20 | 42 | 86 | 65 | -39 | -7 | 2.73 | 3.68 | 1 | 0 | fail | no | - |
| x_climax_all | 21 | 48 | 90 | 71 | -26 | 10 | 3.37 | 3.68 | 8 | 1 | fail | no | - |
| x_climax_half | 20 | 44 | 86 | 70 | -35 | -1 | 2.92 | 3.68 | 4 | 1 | fail | no | - |
| x_euphoria_all | 20 | 55 | 86 | 65 | -30 | 7 | 3.62 | 3.68 | 14 | 2 | fail | no | - |
| x_euphoria_half | 20 | 48 | 86 | 65 | -34 | 0 | 3.18 | 3.68 | 8 | 2 | fail | no | - |
| x_shakeout_hold | 19 | 2 | 79 | 63 | -88 | -49 | 0.06 | 5.94 | -38 | 0 | fail | no | - |
| x_closestop_ctrl | 19 | 15 | 79 | 63 | -68 | -35 | 0.86 | 5.20 | -26 | 0 | fail | no | - |
| ptp_2atr | 20 | 50 | 86 | 70 | -20 | 14 | 3.29 | 3.68 | 19 | 2 | fail | no | - |
| ptp_4atr | 20 | 42 | 86 | 65 | -39 | -7 | 2.73 | 3.68 | 1 | 0 | fail | no | - |

Walk-forward selection over this sleeve's variants (IS fold k-1 -> OOS fold k): n=20 mean=40 median=86 chosen=['baseline', 'of_no_dist', 'baseline', 'x_euphoria_all']

## trendhold@240m  `trendhold(d1=True,ema_fast=20,ema_slow=100)`
OOS days per coin: {'BTC/USD': np.float64(80.5), 'ETH/USD': np.float64(80.5), 'SOL/USD': np.float64(80.5)}; buy-and-hold over the OOS window: mean 27.17% (per coin [24.81, 21.2, 35.49]), mean max DD 14.07%
backtest_core cross-check (baseline OOS n/mean per coin): BTC 6/193, ETH 7/281, SOL 6/461

| variant | n | mean bps | median | win% | lb90 | ex-best2 | OOS ret% | maxDD% | stress mean | folds>base | bar | improves | candidate |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 19 | 310 | -181 | 32 | 8 | 31 | 2.37 | 0.65 | 228 | - | fail | - | - |
| of_no_dist | 19 | 301 | -195 | 32 | -3 | 21 | 2.30 | 0.68 | 219 | 0 | fail | no | - |
| of_no_bear_abs | 19 | 310 | -181 | 32 | 7 | 30 | 2.36 | 0.65 | 227 | 0 | fail | no | - |
| of_abs_pref | 12 | -161 | -238 | 8 | -260 | -246 | -0.77 | 0.77 | -239 | 2 | fail | no | - |
| of_not_extended | 19 | 310 | -181 | 32 | 8 | 31 | 2.37 | 0.65 | 228 | 0 | fail | no | - |
| x_climax_all | 36 | 115 | -131 | 47 | -31 | 1 | 1.65 | 1.11 | 34 | 3 | fail | no | - |
| x_climax_half | 19 | 221 | -181 | 32 | -23 | -2 | 1.69 | 0.65 | 140 | 0 | fail | no | - |
| x_euphoria_all | 38 | 54 | 65 | 53 | -33 | 4 | 0.81 | 1.22 | -27 | 2 | fail | no | - |
| x_euphoria_half | 19 | 174 | -165 | 32 | -26 | -1 | 1.32 | 0.65 | 93 | 1 | fail | no | - |
| x_shakeout_hold | 19 | 301 | -181 | 32 | 3 | 29 | 2.30 | 0.65 | 219 | 0 | fail | no | - |
| ptp_2atr | 19 | 132 | -107 | 37 | -35 | -15 | 1.00 | 0.52 | 61 | 0 | fail | no | - |
| ptp_4atr | 19 | 139 | -181 | 32 | -44 | -17 | 1.05 | 0.65 | 64 | 1 | fail | no | - |

Walk-forward selection over this sleeve's variants (IS fold k-1 -> OOS fold k): n=22 mean=274 median=-173 chosen=['baseline', 'baseline', 'x_climax_all', 'x_euphoria_half']

## Descriptive: do flow features rank forward returns? (Spearman IC, 1h bars; not used for selection)
| feature | horizon | first half BTC/ETH/SOL | second half BTC/ETH/SOL |
|---|---|---|---|
| imb_60 | 4h | -0.021/-0.024/-0.026 | 0.009/-0.017/-0.028 |
| imb_60 | 24h | -0.006/-0.026/-0.015 | 0.026/0.017/0.011 |
| net_z_60 | 4h | -0.038/-0.032/-0.023 | 0.016/-0.018/-0.020 |
| net_z_60 | 24h | 0.004/-0.043/-0.011 | 0.028/0.024/0.007 |
| lnet_z_60 | 4h | -0.022/-0.016/-0.038 | 0.008/-0.023/-0.018 |
| lnet_z_60 | 24h | 0.024/-0.047/0.002 | 0.042/-0.020/0.017 |
| imb_240 | 4h | -0.007/0.008/0.010 | 0.037/-0.020/-0.063 |
| imb_240 | 24h | -0.026/-0.017/-0.033 | 0.050/0.065/0.023 |
| net_z_240 | 4h | -0.026/-0.020/0.004 | 0.024/-0.035/-0.060 |
| net_z_240 | 24h | -0.020/-0.031/-0.028 | 0.045/0.064/0.011 |
| lnet_z_240 | 4h | -0.020/-0.051/-0.010 | 0.031/-0.060/-0.046 |
| lnet_z_240 | 24h | 0.008/-0.069/-0.002 | 0.062/-0.006/0.015 |
| vwap_dev_z | 4h | -0.079/-0.088/-0.048 | 0.025/-0.021/0.031 |
| vwap_dev_z | 24h | 0.018/0.035/0.061 | -0.040/-0.060/0.001 |
| ret_z_240 | 4h | -0.078/-0.110/-0.106 | -0.041/-0.040/-0.017 |
| ret_z_240 | 24h | -0.027/-0.037/0.002 | 0.003/-0.007/0.055 |

Mean forward return (bps) after 15m events:

| coin | event | n | fwd 4h | fwd 24h |
|---|---|---|---|---|
| BTC/USD | unconditional | 12960 | 3 | 20 |
| BTC/USD | buy_climax | 24 | -18 | -51 |
| BTC/USD | sell_climax | 38 | 35 | 26 |
| BTC/USD | euphoria | 40 | 42 | 265 |
| BTC/USD | dip_absorbed | 479 | 8 | 5 |
| ETH/USD | unconditional | 12960 | 4 | 27 |
| ETH/USD | buy_climax | 24 | -7 | 31 |
| ETH/USD | sell_climax | 34 | 15 | 39 |
| ETH/USD | euphoria | 72 | 94 | 244 |
| ETH/USD | dip_absorbed | 614 | 10 | 19 |
| SOL/USD | unconditional | 12960 | 5 | 33 |
| SOL/USD | buy_climax | 18 | -3 | 136 |
| SOL/USD | sell_climax | 26 | 41 | 6 |
| SOL/USD | euphoria | 77 | 27 | 132 |
| SOL/USD | dip_absorbed | 539 | 13 | 23 |

Shadow candidates (bar AND improvement gate): none

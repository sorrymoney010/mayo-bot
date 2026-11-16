# Order-flow filters + exit timers — 2026-11-01T23:22:10Z

Paper research only. Ticks read-only; no lock, setting or live process touched.
Costs: taker 40 + slip 10 bps/side, maker 25; stress 80+10. Configs tried: 35 (pre-registered, fixed thresholds, one run).

- data BTC/USD: longest contiguous tick run 2026-06-04 21:00 .. 2026-10-25 05:00 UTC (142.3 d)
- data ETH/USD: longest contiguous tick run 2026-06-04 21:00 .. 2026-10-25 05:00 UTC (142.3 d)
- data SOL/USD: longest contiguous tick run 2026-06-04 21:00 .. 2026-10-25 05:00 UTC (142.3 d)

## regime@60m  `regime(adx_enter=25.0,adx_exit=20.0,atr_mult=3.0,d1=True,lookback=20,min_atr_rank=0.0,stop=0.03,tp=0.25)`
OOS days per coin: {'BTC/USD': np.float64(106.8), 'ETH/USD': np.float64(106.8), 'SOL/USD': np.float64(106.8)}; buy-and-hold over the OOS window: mean 28.66% (per coin [25.12, 32.14, 28.72]), mean max DD 15.25%
backtest_core cross-check (baseline OOS n/mean per coin): BTC 11/46, ETH 13/69, SOL 9/370

| variant | n | mean bps | median | win% | lb90 | ex-best2 | OOS ret% | maxDD% | stress mean | folds>base | bar | improves | candidate |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 33 | 144 | -141 | 30 | -18 | -2 | 16.07 | 5.59 | 66 | - | fail | - | - |
| of_no_dist | 32 | 152 | -142 | 31 | -14 | 2 | 16.62 | 5.59 | 75 | 1 | fail | no | - |
| of_no_bear_abs | 33 | 144 | -141 | 30 | -18 | -2 | 16.07 | 5.59 | 66 | 0 | fail | no | - |
| of_abs_pref | 0 | - | - | - | - | - | 0.00 | 0.00 | - | 0 | fail | no | - |
| of_not_extended | 30 | 53 | -139 | 33 | -49 | -32 | 5.11 | 3.56 | -27 | 1 | fail | no | - |
| x_climax_all | 37 | 56 | -141 | 35 | -48 | -35 | 6.62 | 8.15 | -24 | 2 | fail | no | - |
| x_climax_half | 33 | 100 | -141 | 33 | -31 | -13 | 10.96 | 5.68 | 21 | 2 | fail | no | - |
| x_euphoria_all | 44 | 10 | -86 | 43 | -42 | -16 | 1.29 | 5.49 | -70 | 0 | fail | no | - |
| x_euphoria_half | 33 | 52 | -141 | 30 | -55 | -38 | 5.43 | 5.81 | -27 | 0 | fail | no | - |
| x_shakeout_hold | 33 | 145 | -141 | 30 | -16 | -0 | 16.31 | 5.40 | 68 | 1 | fail | no | - |
| x_closestop_ctrl | 33 | 145 | -141 | 30 | -17 | -1 | 16.22 | 5.47 | 67 | 1 | fail | no | - |
| ptp_2atr | 33 | 37 | -82 | 30 | -52 | -38 | 3.92 | 5.71 | -29 | 2 | fail | no | - |
| ptp_4atr | 33 | 40 | -141 | 33 | -56 | -39 | 4.14 | 5.56 | -31 | 2 | fail | no | - |

Walk-forward selection over this sleeve's variants (IS fold k-1 -> OOS fold k): n=33 mean=144 median=-141 chosen=['baseline', 'baseline', 'of_no_dist', 'of_not_extended']

## meanrev_mk@240m  `meanrev(d1=True,entry=limit,limit_offset=0.001,rsi_exit=55.0,rsi_os=38.0,stop=0.03,tp=0.25)`
OOS days per coin: {'BTC/USD': np.float64(85.7), 'ETH/USD': np.float64(85.7), 'SOL/USD': np.float64(85.7)}; buy-and-hold over the OOS window: mean 28.8% (per coin [25.13, 23.69, 37.59]), mean max DD 14.92%
backtest_core cross-check (baseline OOS n/mean per coin): BTC 9/7, ETH 6/202, SOL 5/-50

| variant | n | mean bps | median | win% | lb90 | ex-best2 | OOS ret% | maxDD% | stress mean | folds>base | bar | improves | candidate |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 20 | 51 | 86 | 70 | -29 | 3 | 3.38 | 3.68 | 11 | - | fail | - | - |
| of_no_dist | 20 | 19 | 64 | 65 | -61 | -25 | 1.19 | 6.06 | -21 | 2 | fail | no | - |
| of_no_bear_abs | 20 | 51 | 86 | 70 | -29 | 3 | 3.38 | 3.68 | 11 | 0 | fail | no | - |
| of_abs_pref | 12 | -11 | 106 | 58 | -131 | -92 | -0.49 | 4.88 | -51 | 1 | fail | no | - |
| of_not_extended | 20 | 51 | 86 | 70 | -29 | 3 | 3.38 | 3.68 | 11 | 0 | fail | no | - |
| x_climax_all | 21 | 58 | 90 | 76 | -16 | 20 | 4.03 | 3.68 | 17 | 1 | fail | no | - |
| x_climax_half | 20 | 54 | 86 | 75 | -24 | 10 | 3.57 | 3.68 | 13 | 1 | fail | no | - |
| x_euphoria_all | 20 | 64 | 86 | 70 | -20 | 18 | 4.28 | 3.68 | 24 | 2 | fail | no | - |
| x_euphoria_half | 20 | 58 | 86 | 70 | -24 | 11 | 3.83 | 3.68 | 17 | 2 | fail | no | - |
| x_shakeout_hold | 19 | 12 | 79 | 68 | -77 | -38 | 0.70 | 5.94 | -28 | 0 | fail | no | - |
| x_closestop_ctrl | 19 | 25 | 79 | 68 | -57 | -24 | 1.50 | 5.20 | -16 | 0 | fail | no | - |
| ptp_2atr | 20 | 59 | 86 | 75 | -9 | 25 | 3.95 | 3.68 | 29 | 3 | fail | yes | - |
| ptp_4atr | 20 | 51 | 86 | 70 | -29 | 3 | 3.38 | 3.68 | 11 | 0 | fail | no | - |

Walk-forward selection over this sleeve's variants (IS fold k-1 -> OOS fold k): n=20 mean=8 median=64 chosen=['baseline', 'of_no_dist', 'x_euphoria_all', 'of_no_dist']

## trendhold@240m  `trendhold(d1=True,ema_fast=20,ema_slow=100)`
OOS days per coin: {'BTC/USD': np.float64(85.7), 'ETH/USD': np.float64(85.7), 'SOL/USD': np.float64(85.7)}; buy-and-hold over the OOS window: mean 28.8% (per coin [25.13, 23.69, 37.59]), mean max DD 14.92%
backtest_core cross-check (baseline OOS n/mean per coin): BTC 7/113, ETH 7/281, SOL 6/461

| variant | n | mean bps | median | win% | lb90 | ex-best2 | OOS ret% | maxDD% | stress mean | folds>base | bar | improves | candidate |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 20 | 276 | -188 | 30 | -14 | 9 | 2.22 | 0.65 | 194 | - | fail | - | - |
| of_no_dist | 20 | 268 | -204 | 30 | -24 | -1 | 2.15 | 0.68 | 186 | 0 | fail | no | - |
| of_no_bear_abs | 20 | 278 | -192 | 30 | -12 | 11 | 2.23 | 0.65 | 196 | 1 | fail | no | - |
| of_abs_pref | 12 | -161 | -238 | 8 | -260 | -246 | -0.77 | 0.77 | -239 | 2 | fail | no | - |
| of_not_extended | 20 | 276 | -188 | 30 | -14 | 9 | 2.22 | 0.65 | 194 | 0 | fail | no | - |
| x_climax_all | 37 | 102 | -155 | 46 | -41 | -10 | 1.50 | 1.11 | 21 | 0 | fail | no | - |
| x_climax_half | 20 | 192 | -188 | 30 | -43 | -23 | 1.54 | 0.65 | 110 | 0 | fail | no | - |
| x_euphoria_all | 39 | 43 | 64 | 51 | -43 | -6 | 0.66 | 1.22 | -37 | 1 | fail | no | - |
| x_euphoria_half | 20 | 146 | -173 | 30 | -46 | -22 | 1.17 | 0.65 | 66 | 1 | fail | no | - |
| x_shakeout_hold | 20 | 267 | -188 | 30 | -18 | 6 | 2.14 | 0.65 | 185 | 0 | fail | no | - |
| ptp_2atr | 20 | 107 | -131 | 35 | -55 | -35 | 0.85 | 0.52 | 36 | 0 | fail | no | - |
| ptp_4atr | 20 | 113 | -188 | 30 | -64 | -36 | 0.90 | 0.65 | 39 | 1 | fail | no | - |

Walk-forward selection over this sleeve's variants (IS fold k-1 -> OOS fold k): n=17 mean=237 median=-195 chosen=['baseline', 'baseline', 'of_abs_pref', 'x_euphoria_half']

## Descriptive: do flow features rank forward returns? (Spearman IC, 1h bars; not used for selection)
| feature | horizon | first half BTC/ETH/SOL | second half BTC/ETH/SOL |
|---|---|---|---|
| imb_60 | 4h | -0.024/-0.024/-0.027 | 0.021/-0.015/-0.035 |
| imb_60 | 24h | 0.003/-0.011/-0.007 | 0.014/-0.021/-0.013 |
| net_z_60 | 4h | -0.042/-0.035/-0.026 | 0.028/-0.012/-0.028 |
| net_z_60 | 24h | 0.013/-0.027/-0.004 | 0.014/-0.010/-0.013 |
| lnet_z_60 | 4h | -0.026/-0.024/-0.038 | 0.021/-0.015/-0.033 |
| lnet_z_60 | 24h | 0.031/-0.041/0.005 | 0.027/-0.039/-0.004 |
| imb_240 | 4h | -0.014/0.007/0.005 | 0.060/-0.021/-0.085 |
| imb_240 | 24h | -0.008/0.013/-0.021 | 0.028/0.002/-0.016 |
| net_z_240 | 4h | -0.031/-0.023/-0.000 | 0.049/-0.038/-0.081 |
| net_z_240 | 24h | 0.001/-0.003/-0.013 | 0.022/0.008/-0.019 |
| lnet_z_240 | 4h | -0.023/-0.059/-0.011 | 0.058/-0.059/-0.086 |
| lnet_z_240 | 24h | 0.033/-0.055/0.006 | 0.024/-0.042/-0.023 |
| vwap_dev_z | 4h | -0.077/-0.095/-0.045 | 0.038/-0.013/0.029 |
| vwap_dev_z | 24h | 0.029/0.039/0.090 | -0.076/-0.088/-0.042 |
| ret_z_240 | 4h | -0.075/-0.116/-0.104 | -0.019/-0.017/-0.004 |
| ret_z_240 | 24h | -0.023/-0.034/0.009 | -0.006/-0.016/0.040 |

Mean forward return (bps) after 15m events:

| coin | event | n | fwd 4h | fwd 24h |
|---|---|---|---|---|
| BTC/USD | unconditional | 13568 | 3 | 18 |
| BTC/USD | buy_climax | 27 | -13 | -57 |
| BTC/USD | sell_climax | 41 | 31 | 27 |
| BTC/USD | euphoria | 40 | 42 | 265 |
| BTC/USD | dip_absorbed | 505 | 7 | 4 |
| ETH/USD | unconditional | 13568 | 3 | 24 |
| ETH/USD | buy_climax | 24 | -7 | 31 |
| ETH/USD | sell_climax | 35 | 12 | 36 |
| ETH/USD | euphoria | 72 | 94 | 244 |
| ETH/USD | dip_absorbed | 631 | 11 | 20 |
| SOL/USD | unconditional | 13568 | 5 | 30 |
| SOL/USD | buy_climax | 18 | -3 | 136 |
| SOL/USD | sell_climax | 28 | 40 | 19 |
| SOL/USD | euphoria | 77 | 27 | 132 |
| SOL/USD | dip_absorbed | 569 | 15 | 24 |

Shadow candidates (bar AND improvement gate): none

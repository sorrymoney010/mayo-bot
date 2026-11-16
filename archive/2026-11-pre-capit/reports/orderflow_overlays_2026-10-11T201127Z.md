# Order-flow filters + exit timers — 2026-10-11T20:11:26Z

Paper research only. Ticks read-only; no lock, setting or live process touched.
Costs: taker 40 + slip 10 bps/side, maker 25; stress 80+10. Configs tried: 35 (pre-registered, fixed thresholds, one run).

- data BTC/USD: longest contiguous tick run 2026-06-04 21:00 .. 2026-10-05 23:00 UTC (123.1 d)
- data ETH/USD: longest contiguous tick run 2026-06-04 21:00 .. 2026-10-04 14:00 UTC (121.7 d)
- data SOL/USD: longest contiguous tick run 2026-06-04 21:00 .. 2026-10-04 14:00 UTC (121.7 d)

## regime@60m  `regime(adx_enter=25.0,adx_exit=20.0,atr_mult=3.0,d1=True,lookback=20,min_atr_rank=0.0,stop=0.03,tp=0.25)`
OOS days per coin: {'BTC/USD': np.float64(91.5), 'ETH/USD': np.float64(90.3), 'SOL/USD': np.float64(90.3)}; buy-and-hold over the OOS window: mean 45.07% (per coin [35.55, 50.44, 49.22]), mean max DD 10.37%
backtest_core cross-check (baseline OOS n/mean per coin): BTC 10/65, ETH 12/92, SOL 9/382

| variant | n | mean bps | median | win% | lb90 | ex-best2 | OOS ret% | maxDD% | stress mean | folds>base | bar | improves | candidate |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 31 | 168 | -136 | 32 | -4 | 13 | 17.85 | 5.59 | 90 | - | fail | - | - |
| of_no_dist | 30 | 178 | -135 | 33 | 1 | 19 | 18.40 | 5.59 | 100 | 1 | PASS | no | - |
| of_no_bear_abs | 31 | 168 | -136 | 32 | -4 | 13 | 17.85 | 5.59 | 90 | 0 | fail | no | - |
| of_abs_pref | 0 | - | - | - | - | - | 0.00 | 0.00 | - | 0 | fail | no | - |
| of_not_extended | 28 | 73 | -135 | 36 | -35 | -17 | 6.72 | 3.56 | -8 | 1 | fail | no | - |
| x_climax_all | 35 | 72 | -136 | 37 | -37 | -23 | 8.25 | 8.15 | -8 | 2 | fail | no | - |
| x_climax_half | 31 | 121 | -136 | 36 | -18 | 2 | 12.67 | 5.68 | 42 | 2 | fail | no | - |
| x_euphoria_all | 42 | 21 | -65 | 45 | -32 | -6 | 2.85 | 5.49 | -59 | 1 | fail | no | - |
| x_euphoria_half | 31 | 70 | -136 | 32 | -43 | -25 | 7.05 | 5.81 | -9 | 0 | fail | no | - |
| x_shakeout_hold | 31 | 170 | -136 | 32 | -1 | 15 | 18.09 | 5.40 | 92 | 1 | fail | no | - |
| x_closestop_ctrl | 31 | 169 | -136 | 32 | -2 | 14 | 18.00 | 5.47 | 91 | 1 | fail | no | - |
| ptp_2atr | 31 | 52 | -78 | 32 | -42 | -28 | 5.23 | 5.71 | -14 | 1 | fail | no | - |
| ptp_4atr | 31 | 57 | -134 | 36 | -44 | -26 | 5.74 | 5.56 | -14 | 2 | fail | no | - |

Walk-forward selection over this sleeve's variants (IS fold k-1 -> OOS fold k): n=31 mean=161 median=-138 chosen=['baseline', 'baseline', 'of_no_dist', 'of_not_extended']

## meanrev_mk@240m  `meanrev(d1=True,entry=limit,limit_offset=0.001,rsi_exit=55.0,rsi_os=38.0,stop=0.03,tp=0.25)`
OOS days per coin: {'BTC/USD': np.float64(70.2), 'ETH/USD': np.float64(69.2), 'SOL/USD': np.float64(69.2)}; buy-and-hold over the OOS window: mean 41.64% (per coin [30.48, 37.24, 57.2]), mean max DD 8.52%
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
OOS days per coin: {'BTC/USD': np.float64(70.2), 'ETH/USD': np.float64(69.2), 'SOL/USD': np.float64(69.2)}; buy-and-hold over the OOS window: mean 41.64% (per coin [30.48, 37.24, 57.2]), mean max DD 8.52%
backtest_core cross-check (baseline OOS n/mean per coin): BTC 6/234, ETH 7/332, SOL 6/531

| variant | n | mean bps | median | win% | lb90 | ex-best2 | OOS ret% | maxDD% | stress mean | folds>base | bar | improves | candidate |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 19 | 367 | -181 | 32 | 57 | 95 | 2.81 | 0.65 | 285 | - | fail | - | - |
| of_no_dist | 19 | 358 | -195 | 32 | 46 | 85 | 2.74 | 0.68 | 276 | 0 | fail | no | - |
| of_no_bear_abs | 19 | 367 | -181 | 32 | 56 | 94 | 2.81 | 0.65 | 284 | 0 | fail | no | - |
| of_abs_pref | 13 | -107 | -199 | 23 | -198 | -191 | -0.56 | 0.62 | -186 | 0 | fail | no | - |
| of_not_extended | 19 | 367 | -181 | 32 | 57 | 95 | 2.81 | 0.65 | 285 | 0 | fail | no | - |
| x_climax_all | 35 | 149 | -62 | 49 | 2 | 34 | 2.09 | 1.11 | 68 | 0 | PASS | no | - |
| x_climax_half | 19 | 250 | -181 | 32 | 2 | 30 | 1.91 | 0.65 | 168 | 0 | fail | no | - |
| x_euphoria_all | 38 | 80 | 99 | 55 | -8 | 33 | 1.22 | 1.22 | 0 | 0 | fail | no | - |
| x_euphoria_half | 19 | 202 | -165 | 32 | -3 | 31 | 1.54 | 0.65 | 121 | 1 | fail | no | - |
| x_shakeout_hold | 19 | 360 | -181 | 32 | 55 | 95 | 2.76 | 0.65 | 278 | 0 | fail | no | - |
| ptp_2atr | 19 | 161 | -107 | 37 | -13 | 17 | 1.22 | 0.52 | 89 | 2 | fail | no | - |
| ptp_4atr | 19 | 167 | -181 | 32 | -23 | 15 | 1.27 | 0.65 | 93 | 0 | fail | no | - |

Walk-forward selection over this sleeve's variants (IS fold k-1 -> OOS fold k): n=19 mean=132 median=-181 chosen=['baseline', 'ptp_2atr', 'baseline', 'ptp_2atr']

## Descriptive: do flow features rank forward returns? (Spearman IC, 1h bars; not used for selection)
| feature | horizon | first half BTC/ETH/SOL | second half BTC/ETH/SOL |
|---|---|---|---|
| imb_60 | 4h | -0.024/-0.016/-0.024 | 0.018/-0.044/-0.016 |
| imb_60 | 24h | -0.006/-0.025/-0.021 | 0.019/-0.021/0.009 |
| net_z_60 | 4h | -0.043/-0.024/-0.024 | 0.026/-0.048/-0.005 |
| net_z_60 | 24h | 0.005/-0.042/-0.016 | 0.020/-0.011/0.005 |
| lnet_z_60 | 4h | -0.018/-0.007/-0.036 | 0.002/-0.051/-0.004 |
| lnet_z_60 | 24h | 0.027/-0.046/-0.002 | 0.030/-0.047/0.027 |
| imb_240 | 4h | -0.011/0.031/0.023 | 0.041/-0.086/-0.061 |
| imb_240 | 24h | -0.024/-0.018/-0.039 | 0.034/0.018/0.004 |
| net_z_240 | 4h | -0.030/0.003/0.014 | 0.029/-0.101/-0.053 |
| net_z_240 | 24h | -0.021/-0.034/-0.030 | 0.022/0.029/-0.012 |
| lnet_z_240 | 4h | -0.016/-0.028/-0.005 | 0.014/-0.118/-0.036 |
| lnet_z_240 | 24h | 0.010/-0.076/-0.004 | 0.039/-0.046/0.012 |
| vwap_dev_z | 4h | -0.066/-0.073/-0.029 | 0.015/-0.040/0.031 |
| vwap_dev_z | 24h | -0.000/0.058/0.088 | 0.001/-0.077/-0.005 |
| ret_z_240 | 4h | -0.073/-0.105/-0.091 | -0.028/-0.062/-0.018 |
| ret_z_240 | 24h | -0.034/-0.032/0.016 | 0.018/-0.016/0.058 |

Mean forward return (bps) after 15m events:

| coin | event | n | fwd 4h | fwd 24h |
|---|---|---|---|---|
| BTC/USD | unconditional | 11720 | 4 | 27 |
| BTC/USD | buy_climax | 23 | -15 | -43 |
| BTC/USD | sell_climax | 30 | 34 | 15 |
| BTC/USD | euphoria | 40 | 42 | 265 |
| BTC/USD | dip_absorbed | 409 | 8 | 9 |
| ETH/USD | unconditional | 11588 | 6 | 40 |
| ETH/USD | buy_climax | 22 | -2 | 60 |
| ETH/USD | sell_climax | 26 | -3 | 18 |
| ETH/USD | euphoria | 67 | 104 | 276 |
| ETH/USD | dip_absorbed | 515 | 10 | 22 |
| SOL/USD | unconditional | 11588 | 8 | 50 |
| SOL/USD | buy_climax | 17 | -4 | 170 |
| SOL/USD | sell_climax | 20 | 31 | 15 |
| SOL/USD | euphoria | 77 | 27 | 132 |
| SOL/USD | dip_absorbed | 474 | 10 | 41 |

Shadow candidates (bar AND improvement gate): none

# Order-flow filters + exit timers — 2026-10-11T20:31:43Z

Paper research only. Ticks read-only; no lock, setting or live process touched.
Costs: taker 40 + slip 10 bps/side, maker 25; stress 80+10. Configs tried: 35 (pre-registered, fixed thresholds, one run).

- data BTC/USD: longest contiguous tick run 2026-06-04 21:00 .. 2026-10-11 20:00 UTC (129.0 d)
- data ETH/USD: longest contiguous tick run 2026-06-04 21:00 .. 2026-10-11 20:00 UTC (129.0 d)
- data SOL/USD: longest contiguous tick run 2026-06-04 21:00 .. 2026-10-11 20:00 UTC (129.0 d)

## regime@60m  `regime(adx_enter=25.0,adx_exit=20.0,atr_mult=3.0,d1=True,lookback=20,min_atr_rank=0.0,stop=0.03,tp=0.25)`
OOS days per coin: {'BTC/USD': np.float64(96.1), 'ETH/USD': np.float64(96.1), 'SOL/USD': np.float64(96.1)}; buy-and-hold over the OOS window: mean 34.49% (per coin [29.55, 39.71, 34.21]), mean max DD 11.87%
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
| x_euphoria_all | 43 | 14 | -75 | 44 | -39 | -13 | 1.79 | 5.49 | -66 | 1 | fail | no | - |
| x_euphoria_half | 32 | 58 | -139 | 31 | -52 | -35 | 5.94 | 5.81 | -21 | 0 | fail | no | - |
| x_shakeout_hold | 32 | 154 | -139 | 31 | -12 | 4 | 16.87 | 5.40 | 77 | 1 | fail | no | - |
| x_closestop_ctrl | 32 | 154 | -139 | 31 | -12 | 4 | 16.78 | 5.47 | 76 | 1 | fail | no | - |
| ptp_2atr | 32 | 41 | -84 | 31 | -51 | -38 | 4.15 | 5.71 | -26 | 1 | fail | no | - |
| ptp_4atr | 32 | 46 | -139 | 34 | -53 | -35 | 4.65 | 5.56 | -25 | 2 | fail | no | - |

Walk-forward selection over this sleeve's variants (IS fold k-1 -> OOS fold k): n=32 mean=146 median=-144 chosen=['baseline', 'baseline', 'of_no_dist', 'of_not_extended']

## meanrev_mk@240m  `meanrev(d1=True,entry=limit,limit_offset=0.001,rsi_exit=55.0,rsi_os=38.0,stop=0.03,tp=0.25)`
OOS days per coin: {'BTC/USD': np.float64(75.0), 'ETH/USD': np.float64(75.0), 'SOL/USD': np.float64(75.0)}; buy-and-hold over the OOS window: mean 36.23% (per coin [29.57, 30.81, 48.29]), mean max DD 10.83%
backtest_core cross-check (baseline OOS n/mean per coin): BTC 7/-24, ETH 6/184, SOL 5/61

| variant | n | mean bps | median | win% | lb90 | ex-best2 | OOS ret% | maxDD% | stress mean | folds>base | bar | improves | candidate |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 18 | 69 | 106 | 72 | -11 | 23 | 4.16 | 3.68 | 29 | - | fail | - | - |
| of_no_dist | 18 | 37 | 107 | 67 | -46 | -10 | 2.17 | 4.88 | -3 | 2 | fail | no | - |
| of_no_bear_abs | 18 | 69 | 106 | 72 | -11 | 23 | 4.16 | 3.68 | 29 | 0 | fail | no | - |
| of_abs_pref | 11 | -28 | 92 | 55 | -158 | -122 | -1.09 | 4.88 | -68 | 2 | fail | no | - |
| of_not_extended | 18 | 69 | 106 | 72 | -11 | 23 | 4.16 | 3.68 | 29 | 0 | fail | no | - |
| x_climax_all | 19 | 75 | 92 | 79 | 2 | 40 | 4.81 | 3.68 | 35 | 1 | fail | no | - |
| x_climax_half | 18 | 72 | 106 | 78 | -6 | 31 | 4.35 | 3.68 | 32 | 1 | fail | no | - |
| x_euphoria_all | 18 | 84 | 106 | 72 | -1 | 34 | 5.06 | 3.68 | 43 | 2 | fail | no | - |
| x_euphoria_half | 18 | 76 | 106 | 72 | -6 | 31 | 4.61 | 3.68 | 36 | 2 | fail | no | - |
| x_shakeout_hold | 17 | 51 | 92 | 71 | -32 | -0 | 2.87 | 3.79 | 11 | 0 | fail | no | - |
| x_closestop_ctrl | 17 | 51 | 92 | 71 | -32 | -0 | 2.87 | 3.79 | 11 | 0 | fail | no | - |
| ptp_2atr | 18 | 67 | 106 | 78 | -8 | 33 | 4.06 | 3.68 | 37 | 1 | fail | no | - |
| ptp_4atr | 18 | 69 | 106 | 72 | -11 | 23 | 4.16 | 3.68 | 29 | 0 | fail | no | - |

Walk-forward selection over this sleeve's variants (IS fold k-1 -> OOS fold k): n=18 mean=68 median=106 chosen=['baseline', 'of_no_dist', 'baseline', 'x_euphoria_all']

## trendhold@240m  `trendhold(d1=True,ema_fast=20,ema_slow=100)`
OOS days per coin: {'BTC/USD': np.float64(75.0), 'ETH/USD': np.float64(75.0), 'SOL/USD': np.float64(75.0)}; buy-and-hold over the OOS window: mean 36.23% (per coin [29.57, 30.81, 48.29]), mean max DD 10.83%
backtest_core cross-check (baseline OOS n/mean per coin): BTC 6/193, ETH 7/281, SOL 6/461

| variant | n | mean bps | median | win% | lb90 | ex-best2 | OOS ret% | maxDD% | stress mean | folds>base | bar | improves | candidate |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 19 | 310 | -181 | 32 | 8 | 31 | 2.37 | 0.65 | 228 | - | fail | - | - |
| of_no_dist | 19 | 301 | -195 | 32 | -3 | 21 | 2.30 | 0.68 | 219 | 0 | fail | no | - |
| of_no_bear_abs | 19 | 310 | -181 | 32 | 7 | 30 | 2.36 | 0.65 | 227 | 0 | fail | no | - |
| of_abs_pref | 12 | -161 | -238 | 8 | -260 | -246 | -0.77 | 0.77 | -239 | 1 | fail | no | - |
| of_not_extended | 19 | 310 | -181 | 32 | 8 | 31 | 2.37 | 0.65 | 228 | 0 | fail | no | - |
| x_climax_all | 36 | 115 | -131 | 47 | -31 | 1 | 1.65 | 1.11 | 34 | 1 | fail | no | - |
| x_climax_half | 19 | 221 | -181 | 32 | -23 | -2 | 1.69 | 0.65 | 140 | 0 | fail | no | - |
| x_euphoria_all | 38 | 54 | 65 | 53 | -33 | 4 | 0.81 | 1.22 | -27 | 1 | fail | no | - |
| x_euphoria_half | 19 | 174 | -165 | 32 | -26 | -1 | 1.32 | 0.65 | 93 | 1 | fail | no | - |
| x_shakeout_hold | 19 | 301 | -181 | 32 | 3 | 29 | 2.30 | 0.65 | 219 | 0 | fail | no | - |
| ptp_2atr | 19 | 132 | -107 | 37 | -35 | -15 | 1.00 | 0.52 | 61 | 1 | fail | no | - |
| ptp_4atr | 19 | 139 | -181 | 32 | -44 | -17 | 1.05 | 0.65 | 64 | 1 | fail | no | - |

Walk-forward selection over this sleeve's variants (IS fold k-1 -> OOS fold k): n=19 mean=126 median=-181 chosen=['baseline', 'ptp_2atr', 'baseline', 'x_euphoria_half']

## Descriptive: do flow features rank forward returns? (Spearman IC, 1h bars; not used for selection)
| feature | horizon | first half BTC/ETH/SOL | second half BTC/ETH/SOL |
|---|---|---|---|
| imb_60 | 4h | -0.022/-0.026/-0.031 | 0.028/-0.018/-0.016 |
| imb_60 | 24h | -0.005/-0.026/-0.023 | 0.036/0.013/0.017 |
| net_z_60 | 4h | -0.040/-0.035/-0.029 | 0.034/-0.022/-0.007 |
| net_z_60 | 24h | 0.005/-0.043/-0.020 | 0.039/0.022/0.014 |
| lnet_z_60 | 4h | -0.019/-0.016/-0.042 | 0.021/-0.021/-0.006 |
| lnet_z_60 | 24h | 0.025/-0.048/-0.005 | 0.059/-0.017/0.033 |
| imb_240 | 4h | -0.006/0.007/-0.004 | 0.053/-0.041/-0.047 |
| imb_240 | 24h | -0.022/-0.016/-0.045 | 0.062/0.065/0.033 |
| net_z_240 | 4h | -0.025/-0.022/-0.010 | 0.041/-0.059/-0.043 |
| net_z_240 | 24h | -0.017/-0.030/-0.039 | 0.057/0.073/0.020 |
| lnet_z_240 | 4h | -0.019/-0.053/-0.025 | 0.046/-0.074/-0.034 |
| lnet_z_240 | 24h | 0.009/-0.069/-0.009 | 0.087/0.005/0.034 |
| vwap_dev_z | 4h | -0.071/-0.081/-0.040 | 0.030/-0.035/0.037 |
| vwap_dev_z | 24h | 0.018/0.042/0.072 | 0.014/-0.014/0.046 |
| ret_z_240 | 4h | -0.075/-0.107/-0.101 | -0.023/-0.048/-0.011 |
| ret_z_240 | 24h | -0.026/-0.032/0.007 | 0.022/0.006/0.077 |

Mean forward return (bps) after 15m events:

| coin | event | n | fwd 4h | fwd 24h |
|---|---|---|---|---|
| BTC/USD | unconditional | 12284 | 4 | 23 |
| BTC/USD | buy_climax | 23 | -15 | -43 |
| BTC/USD | sell_climax | 36 | 28 | 19 |
| BTC/USD | euphoria | 40 | 42 | 265 |
| BTC/USD | dip_absorbed | 451 | 5 | 2 |
| ETH/USD | unconditional | 12284 | 5 | 32 |
| ETH/USD | buy_climax | 23 | -1 | 56 |
| ETH/USD | sell_climax | 29 | -4 | 19 |
| ETH/USD | euphoria | 67 | 104 | 276 |
| ETH/USD | dip_absorbed | 563 | 7 | 13 |
| SOL/USD | unconditional | 12284 | 6 | 40 |
| SOL/USD | buy_climax | 18 | -3 | 136 |
| SOL/USD | sell_climax | 23 | 20 | -22 |
| SOL/USD | euphoria | 77 | 27 | 132 |
| SOL/USD | dip_absorbed | 515 | 9 | 22 |

Shadow candidates (bar AND improvement gate): none

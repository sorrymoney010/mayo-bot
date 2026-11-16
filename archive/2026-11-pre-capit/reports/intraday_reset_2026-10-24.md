# ONE synthesized intraday strategy `mtf_synth` (15m/30m/1h) — 2026-10-24 — PAPER ONLY

## Evidence table (source → rule it contributed)
| # | source | rule(s) taken |
|---|---|---|
| 1 | Mayo/Dublin first commit `.env.example` (git root, Aug 2026) + commit 35e149d "rapid mode (15m)" | 15m base timeframe; EMA20/EMA50/EMA200 trend stack; RSI14 band 45–68; ATR14 x1.5 stop; 20-bar breakout; volume >= 1.10x 20-bar avg; 1% risk; 3% daily loss; 25% max position |
| 2 | Mayo `TrendBreakoutStrategy` exit (src/dublin_bot/strategy.py) | trend exit when price closes below EMA → runner exit on 1h close < 1h EMA20 |
| 3 | /workspace/dual-venue-momentum-bot/README.md "Strategy (locked)" | close above prior 20-bar high with volume > avg on 15m; max 1/symbol, 3 concurrent |
| 4 | user's 2nd bot /workspace/kraken-ema-bot/README_RUN.md (zip attached in Mayo chat) | scale out in thirds at 1R/2R; stop to breakeven after TP1; stop-first on ambiguous bars |
| 5 | Telegram channels (FXG077, blueanalyze; /workspace/mayo-telegram-study/REPORT.md) | "close half / book profit", "move entry to SL" (breakeven). Raw signals NOT traded (all negative after fees) |
| 6 | Pump bot STRATEGY.md (Pump chat attachment) | TP ladder partial sells + max-hold timer → 12h time stop |
| 7 | PDF "Algorithmic Trading Bot", Mathur et al., ITM Web Conf 40, 03041 (2021) doi:10.1051/itmconf/20214003041 (Mayo chat attachment) | Donchian / MA-crossover / golden-cross intraday rules with stop-loss → Donchian 20-bar breakout + EMA cross trend filter |
| 8 | Shen, Urquhart, Wang "Bitcoin intraday time-series momentum" (doi:10.1111/fire.12290; Kraken among exchanges) and Aloud-style HF study mdpi.com/1911-8074/12/1/36 (predictability up to 6h) | intraday momentum persists on 30m–6h horizons → 30m momentum confirmation + 1h trend filter |
| 9 | Kraken fee schedule kraken.com/features/fee-schedule ; support.kraken.com/articles/cross-platform-fee-tier-changes ; kraken.com/pro/spot (post-only) | maker-first post-only limit entries + maker TPs; fees modeled 40 maker / 80 taker (Tier 1, Jul 2026) |
| 10 | Mayo audits (/workspace/mayo-audit/v2_20261023/REPORT.md, reports/orderflow_findings_2026-10-11.md, /workspace/mayo-rf-study/REPORT.md) | partial TPs alone don't add edge; order-flow overlays not robust → OFI not used as a gate; non-fill & stop-first modeling |
| — | Transcripts searched (ReadTranscript: Mayo, Grok Bot, Pump, Head, Framo, latest 200 msgs each) | no additional strategy specs found (ops/status chatter only) |

## Strategy rules (ONE strategy)
Entry (all on last CLOSED bars): 1h EMA20>EMA50 and 1h close>EMA200 [1,7,8]; 30m close>EMA20 and 30m RSI14 in [45,68] [1,8];
15m close > prior 20-bar high [1,3,7] with 15m volume >= 1.10x 20-bar avg [1,3]. Post-only limit at the 15m close, valid 15 min, fills only if price trades below it [9].
Risk: 1% equity / (1.5xATR14 15m) capped at 25% notional, 1 per coin, max 3 open, 3% daily-loss halt [1,3].
Exit: stop 1.5xATR [1,4]; 1/3 at 1R, 1/3 at 2R as maker [4,5,6]; stop→breakeven after TP1 [4,5]; runner exits on 1h close < 1h EMA20 [2]; 12h time stop [6].

## Backtest of mtf_synth (Kraken Futures PF_* 15m trade candles 2025-10-24→2026-10-24, BTC+ETH+SOL; quarterly walk-forward folds, fixed params)
| min 1R bps | fees m/t | trades | no-fill | win% | gross bps | net bps | PF | quarters + | per-coin net |
|---|---|---|---|---|---|---|---|---|---|
| 0 | 40/80 | 549 | 24 | 8.0 | -13.6 | -124.3 | 0.04 | 0/5 | {'PF_XBTUSD': -121.7, 'PF_ETHUSD': -127.0, 'PF_SOLUSD': -124.4} |
| 0 | 22/38 | 549 | 24 | 14.6 | -13.6 | -69.9 | 0.13 | 0/5 | {'PF_XBTUSD': -67.4, 'PF_ETHUSD': -72.7, 'PF_SOLUSD': -69.9} |
| 100 | 40/80 | 33 | 2 | 21.2 | -39.5 | -151.8 | 0.1 | 0/4 | {'PF_XBTUSD': -152.8, 'PF_ETHUSD': -150.4, 'PF_SOLUSD': -151.9} |
| 100 | 22/38 | 33 | 2 | 21.2 | -39.5 | -96.4 | 0.21 | 0/4 | {'PF_XBTUSD': -98.1, 'PF_ETHUSD': -95.2, 'PF_SOLUSD': -96.4} |

**Honest verdict:** it loses at every fee level; gross edge is already negative (−14 bps/trade), and the Tier-1 round trip (≥80 bps) dwarfs any intraday move the rules capture.
Deployed (min-1R filter off, so it trades ~1.5/day) to PAPER only to collect forward evidence, per the user. Not a validated edge; do not go live on this.

---
## Appendix: earlier single-timeframe variant study (superseded)
# Intraday reset (15m / 30m / 1h) — 2026-10-24 — PAPER ONLY

## Recovered user strategy (sources)
* Mayo/Dublin original `.env.example` (first commit, Aug 2026): `TIMEFRAME_MINUTES=15`, `FAST_EMA=20`, `SLOW_EMA=50`, `REGIME_EMA=200`,
  `RSI_PERIOD=14 RSI_MIN=45 RSI_MAX=68`, `ATR_STOP_MULTIPLIER=1.5`, `BREAKOUT_LOOKBACK=20`, `VOLUME_LOOKBACK=20`, `MIN_VOLUME_RATIO=1.10`; commit 35e149d "rapid mode (15m)".
* /workspace/dual-venue-momentum-bot/README.md "Strategy (locked)": close above prior N=20 bar high with volume > 20-bar avg, 15m, 1% risk, max 1/symbol, 3 concurrent, 3% daily loss.
* /workspace/kraken-ema-bot/README_RUN.md (user's 2nd bot): EMA20 crosses above EMA50 with RSI(14) 50–70; stop 1.5×ATR(14); TPs 1/3 each at 1R/2R/3R; stop to breakeven after TP1.
* Cash-out (user's channels, /workspace/mayo-telegram-study/REPORT.md): "close half trade book profit", "change your entry point to SL" (breakeven).

## Fees (verified 2026-10-24)
Kraken Pro spot since 2026-07-09: Tier 1 ($0+ 30d vol) **maker 0.40% / taker 0.80%**; Tier 3 ($10K+) 0.22/0.38. https://www.kraken.com/features/fee-schedule ,
https://support.kraken.com/articles/cross-platform-fee-tier-changes . The old 25/40 assumption is out of date. Post-only orders always pay maker (kraken.com/pro/spot).

## Backtest (Kraken Futures PF_* 15m trade candles, 2025-10-24 → 2026-10-24, resampled to 30m/1h; BTC+ETH+SOL pooled)
Entry: post-only limit at signal close, filled only if price trades below it within 1 bar (non-fills counted). TP ladder maker; stop/time-stop taker + 10 bps slip; stop assumed first on ambiguous bars.
Folds = calendar quarters (fixed parameters, no fitting).

| tf | variant | fees m/t | trades | no-fill | win% | gross bps | net bps | PF | quarters + |
|---|---|---|---|---|---|---|---|---|---|
| 15m | user_breakout | 40/80 | 562 | 27 | 11.0 | -13.2 | -121.6 | 0.04 | 0/5 |
| 15m | user_breakout | 25/40 | 562 | 27 | 18.1 | -13.2 | -73.8 | 0.11 | 0/5 |
| 15m | user_emacross | 40/80 | 851 | 59 | 11.4 | -9.8 | -118.6 | 0.06 | 0/5 |
| 15m | user_emacross | 25/40 | 851 | 59 | 18.6 | -9.8 | -70.6 | 0.14 | 0/5 |
| 15m | pullback_trend | 40/80 | 1692 | 140 | 11.8 | -8.5 | -116.3 | 0.05 | 0/5 |
| 15m | pullback_trend | 25/40 | 1692 | 140 | 19.3 | -8.5 | -68.9 | 0.13 | 0/5 |
| 30m | user_breakout | 40/80 | 261 | 7 | 13.0 | -17.4 | -125.8 | 0.06 | 0/5 |
| 30m | user_breakout | 25/40 | 261 | 7 | 20.3 | -17.4 | -78.0 | 0.15 | 0/5 |
| 30m | user_emacross | 40/80 | 407 | 16 | 15.0 | -13.2 | -122.8 | 0.07 | 0/5 |
| 30m | user_emacross | 25/40 | 407 | 16 | 20.1 | -13.2 | -74.3 | 0.18 | 0/5 |
| 30m | pullback_trend | 40/80 | 900 | 37 | 15.2 | -10.8 | -119.8 | 0.09 | 0/5 |
| 30m | pullback_trend | 25/40 | 900 | 37 | 22.4 | -10.8 | -71.7 | 0.2 | 0/5 |
| 60m | user_breakout | 40/80 | 123 | 2 | 14.6 | -19.5 | -130.7 | 0.11 | 0/5 |
| 60m | user_breakout | 25/40 | 123 | 2 | 22.0 | -19.5 | -81.2 | 0.22 | 0/5 |
| 60m | user_emacross | 40/80 | 205 | 9 | 15.6 | -13.3 | -124.0 | 0.12 | 0/5 |
| 60m | user_emacross | 25/40 | 205 | 9 | 22.4 | -13.3 | -74.8 | 0.24 | 0/5 |
| 60m | pullback_trend | 40/80 | 490 | 10 | 18.4 | -15.6 | -126.7 | 0.11 | 0/5 |
| 60m | pullback_trend | 25/40 | 490 | 10 | 24.9 | -15.6 | -77.3 | 0.23 | 0/5 |

**Honest verdict:** every cell loses, and *gross* (before fees) expectancy is already negative (−8 to −20 bps/trade).
At Kraken Tier-1 fees the round trip costs ≥80 bps maker-maker, ~120+ bps with a taker stop. Deployed to PAPER per the user to gather live evidence, NOT as a validated edge.

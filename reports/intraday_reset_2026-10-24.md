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

# mtf_synth v2 — "fewer, longer trades" (2026-10-24, America/Chicago)

User decision: one strategy, 15m/30m/1h only, fewer/longer trades each targeting well over 1% to clear
Kraken Tier-1 fees (40 bps maker / 80 bps taker). Research: `research/intraday/bt3.py`,
results `research/intraday/scorecard_fewer_longer.json`. Data: Kraken Futures PF_XBT/ETH/SOL 15m, 12 months.
Costs: maker entry 40, maker target 40, taker stop/trail/time 80 + 10 bps slippage.

## Rules (deployed, PAPER)
- 1h: EMA20>EMA50, close>EMA200, EMA50 rising vs 12h ago, EMA20 rising vs 6h ago; ATR14(1h) > its 100-bar mean.
- 30m: close > EMA20, RSI14 50-70.  15m: close > prior 48-bar (12h) high, volume >= 1.10x 20-bar avg.
- Stop 2.5 x ATR14(1h). Single target at 3R, must be >= 2.5% away (else no trade). Sell half there (maker),
  stop to entry +0.2%; runner exits on 1h close < 1h EMA20; 24h time stop.
- Post-only limit at signal close, good 15 min. 1% risk, max 2 open, max 2 entries/Chicago day, 12h
  per-pair cooldown after a loss, 3% daily loss cap.

## Results (net after fees + slippage, all 3 pairs)
| version | trades | win% | gross bps/tr | net bps/tr | PF | +folds | mean ex best 2 |
|---|---|---|---|---|---|---|---|
| old mtf_synth v1 (40/80) | 549 | 8 | -13.6 | ~-120 | 0.04 | - | - |
| v2 deployed, full year | 97 | 24.7 | -0.1 | -118.5 | 0.38 | 0/6 | -137.3 |
| v2 purged walk-forward OOS (folds 3-6) | 69 | 24.6 | -8.1 | -124.9 | 0.37 | 0/4 | -147.0 |
| best other grid cell (pullback, 3xATR, 72h) | 128 | 29.7 | -34.4 | -150.5 | 0.40 | 0/6 | -167.3 |

Grid (48 cells): entry {breakout, pullback} x stop {2.5,3.0}xATR1h x target {2R,3R} x time stop {24,48,72}h x
ATR-expansion {off,on}. Walk-forward: 6 ~2-month folds, anchored, 3-day purge, select by in-sample net sum.
No cell had a positive fold majority or PF > 0.40.

## Verdict: FAILS the promotion bar (>=30 trades OK; folds 0/4; PF 0.37 < 1.10)
Trades are now fewer (~8/month vs ~46) and longer (avg 17h) and gross is ~breakeven instead of -13.6 bps,
but the ~118 bps round-trip cost dominates: in the last 12 months long-only 15m/30m/1h breakouts on BTC/ETH/SOL
had no gross edge large enough to pay Tier-1 fees. Deployed to PAPER anyway per user request (best fee-aware
version found, highest net bps/trade, and it is the walk-forward pick). Paper evidence only; nothing live.

# Intraday sleeve on Binance.US (PAPER) — 2026-10-25 (America/Chicago)

Venue switch requested by the user ("lets flip to binance" = Binance.US). Fees: 0 bps maker / 2 bps taker
(blog.binance.us/zero-fee-trading) + 3 bps slippage on taker exits. Data: Binance.US public 15m klines,
400 days, BTCUSDT/ETHUSDT/SOLUSDT (USDT books ~1.5-2x the USD books' 24h quote volume, checked 2026-10-25).
Scripts: `research/intraday/fetch_binanceus.py`, `research/intraday/bt4.py`; results
`research/intraday/scorecard_binanceus.json`. Walk-forward: 6 ~2.2-month folds, anchored, 3-day purge,
selection = in-sample net bps sum (>=10 trades), 64-cell grid (16 v1 + 48 v2).

| version | trades | win% | gross bps/tr | net bps/tr | PF | +folds | mean ex best 2 | avg hold |
|---|---|---|---|---|---|---|---|---|
| v1 original (1.5xATR15m, 1/3@1R+2R, 12h), full | 614 | 39.7 | -12.2 | -13.8 | 0.57 | 0/6 | -14.7 | 2.8h |
| v2 as deployed on Kraken (ATR-x on, 24h), full | 117 | 29.1 | -22.6 | -24.5 | 0.79 | 2/6 | -40.8 | 15.3h |
| WF out-of-sample, v1 family | 421 | 37.5 | -12.4 | -14.0 | 0.58 | 0/4 | -16.0 | 3.5h |
| WF out-of-sample, v2 family (= all 64 cells) | 106 | 23.6 | -30.6 | -32.5 | 0.73 | 1/4 | -56.5 | 24.4h |
| **chosen: v2, ATR-x off, 48h (last 3 WF picks), full** | 167 | 29.9 | -12.3 | -14.2 | 0.87 | 2/6 | -29.1 | 24.9h |

Choice: the walk-forward over all 64 cells picked v2 every fold (v1 never won in-sample) and settled on
breakout / 2.5xATR(1h) / 3R / 48h / no ATR filter for the last three folds; it also has the best full-year PF
of the breakout cells. v1's short trades lose ~14 bps gross per trade, so near-zero fees don't rescue it.

**Verdict: FAILS the bar** (trades OK; positive folds 1/4 OOS, 2/6 full; PF 0.73 OOS / 0.87 full < 1.10).
Fees are no longer the problem — gross edge is negative on this venue/period. Deployed to PAPER for evidence.

Feed: Binance.US public WebSocket (aggTrade + bookTicker, port 443; :9443 is unreachable from the box) and
REST aggTrades/klines, ticks in data/binanceus/. Kraken code paths are unchanged (MARKET_VENUE=kraken restores).

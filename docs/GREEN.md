# Green bar (2026-10-04)

Paper stays locked until a sleeve is green. Green is a report in
`logs/promotion_report.json`. It does not flip `ALLOW_LIVE_TRADING`.

A sleeve is green only when all of these are true on closed paper trades
after fees:

1. At least 30 closes.
2. Mean net bps > 0 and the 90% lower bound > 0.
3. Mean without the best two trades > 0.
4. Realized P&L >= 0.
5. The sleeve is not retired.
6. On the same window, its return beats equal-weight buy-and-hold of BTC, ETH, and SOL.

Retired, do not paper-trade these toward a live unlock:

- momentum, at every timeframe
- breakout / momentum_breakout (including the maker variant)
- every 15m sleeve and every daily sleeve
- Kraken PUMP
- futures short
- sr_flip, pattern, elliott_lite

Still paper experiments, not green:

- `regime_trend` @ 60m with the D1 filter
- `meanrev_mk` @ 240m
- `trendhold` @ 240m (lagged buy-and-hold by about 27 points in the last up window)
- `hold_core` (new): long only while D1 is risk-on, flat when it is off. No EMA churn.

`STRATEGY=hold_core` is the paper path that matches the audit. It is not green
until the bar above passes. `docs/LIVE_READY.md` is not an unlock.

# Strategy research 2026-10-03

Paper research only. This run does not change PAPER_TRADING, DRY_RUN,
ALLOW_LIVE_TRADING, the ledger-owner gate, or the single-instance lock.
Momentum and sub-1h entries are not in the library.

Kraken public OHLC is capped near 720 bars (about 30 days at 1h, 120 days at 4h). This is not a multi-year sample.

- regime@60m: OOS trades 8, mean 208.2 bps, return 5.65%, max DD 0.91%, buy-and-hold mean 977.9 bps, promotion not yet (failed: min_trades)
- meanrev@60m: OOS trades 12, mean -6.6 bps, return -0.27%, max DD 1.44%, buy-and-hold mean 977.9 bps, promotion not yet (failed: min_trades, mean_positive, lower_bound_positive, ex_best_2_positive, beats_cash)
- trendhold@60m: OOS trades 40, mean -66.8 bps, return -1.07%, max DD 2.18%, buy-and-hold mean 977.9 bps, promotion not yet (failed: mean_positive, lower_bound_positive, ex_best_2_positive, beats_cash)
- regime@240m: OOS trades 13, mean 77.0 bps, return 3.57%, max DD 7.60%, buy-and-hold mean 4405.7 bps, promotion not yet (failed: min_trades, lower_bound_positive, ex_best_2_positive)
- meanrev@240m: OOS trades 14, mean 94.6 bps, return 4.47%, max DD 1.24%, buy-and-hold mean 4405.7 bps, promotion not yet (failed: min_trades)
- trendhold@240m: OOS trades 19, mean 347.8 bps, return 2.66%, max DD 0.65%, buy-and-hold mean 4405.7 bps, promotion not yet (failed: min_trades)

No candidate cleared the promotion bar. Nothing registered.

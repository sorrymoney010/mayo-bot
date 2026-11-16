# Strategy research 2026-10-11

Paper research only. This run does not change PAPER_TRADING, DRY_RUN,
ALLOW_LIVE_TRADING, the ledger-owner gate, or the single-instance lock.
Momentum and sub-1h entries are not in the library.

Tick-built bars from the data dir (read-only; no bars1m cache written).

- regime@60m: OOS trades 31, mean 152.0 bps, return 16.01%, max DD 3.74%, buy-and-hold mean 5573.6 bps, promotion not yet (failed: lower_bound_positive, ex_best_2_positive)
- meanrev@60m: OOS trades 64, mean -17.6 bps, return -3.78%, max DD 4.53%, buy-and-hold mean 5573.6 bps, promotion not yet (failed: mean_positive, lower_bound_positive, ex_best_2_positive, beats_cash)
- trendhold@60m: OOS trades 94, mean -38.7 bps, return -1.46%, max DD 3.21%, buy-and-hold mean 5573.6 bps, promotion not yet (failed: mean_positive, lower_bound_positive, ex_best_2_positive, beats_cash)
- regime@240m: OOS trades 13, mean 77.0 bps, return 3.57%, max DD 7.60%, buy-and-hold mean 4694.1 bps, promotion not yet (failed: min_trades, lower_bound_positive, ex_best_2_positive)
- meanrev@240m: OOS trades 14, mean 94.6 bps, return 4.47%, max DD 1.24%, buy-and-hold mean 4694.1 bps, promotion not yet (failed: min_trades)
- trendhold@240m: OOS trades 19, mean 361.1 bps, return 2.77%, max DD 0.65%, buy-and-hold mean 4694.1 bps, promotion not yet (failed: min_trades)

No candidate cleared the promotion bar. Nothing registered.

Order-flow overlays (35 pre-registered configs): no shadow candidate; see reports/orderflow_overlays_2026-10-11T200238Z.md

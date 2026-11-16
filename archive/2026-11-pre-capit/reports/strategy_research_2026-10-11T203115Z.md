# Strategy research 2026-10-11

Paper research only. This run does not change PAPER_TRADING, DRY_RUN,
ALLOW_LIVE_TRADING, the ledger-owner gate, or the single-instance lock.
Momentum and sub-1h entries are not in the library.

Tick-built bars from the data dir (read-only; no bars1m cache written).

- regime@60m: OOS trades 33, mean 140.5 bps, return 15.45%, max DD 5.77%, buy-and-hold mean 4658.9 bps, promotion not yet (failed: lower_bound_positive, ex_best_2_positive)
- meanrev@60m: OOS trades 72, mean -27.6 bps, return -6.54%, max DD 8.79%, buy-and-hold mean 4658.9 bps, promotion not yet (failed: mean_positive, lower_bound_positive, ex_best_2_positive, beats_cash)
- trendhold@60m: OOS trades 107, mean -56.3 bps, return -2.40%, max DD 3.91%, buy-and-hold mean 4658.9 bps, promotion not yet (failed: mean_positive, lower_bound_positive, ex_best_2_positive, beats_cash)
- regime@240m: OOS trades 13, mean 81.1 bps, return 3.13%, max DD 8.00%, buy-and-hold mean 3868.6 bps, promotion not yet (failed: min_trades, lower_bound_positive, ex_best_2_positive)
- meanrev@240m: OOS trades 18, mean 69.1 bps, return 4.16%, max DD 3.68%, buy-and-hold mean 3868.6 bps, promotion not yet (failed: min_trades, lower_bound_positive)
- trendhold@240m: OOS trades 19, mean 310.0 bps, return 2.37%, max DD 0.65%, buy-and-hold mean 3868.6 bps, promotion not yet (failed: min_trades)

No candidate cleared the promotion bar. Nothing registered.

Order-flow overlays (35 pre-registered configs): no shadow candidate; see reports/orderflow_overlays_2026-10-11T203144Z.md

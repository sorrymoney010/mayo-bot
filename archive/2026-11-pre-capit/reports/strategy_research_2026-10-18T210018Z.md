# Strategy research 2026-10-18

Paper research only. This run does not change PAPER_TRADING, DRY_RUN,
ALLOW_LIVE_TRADING, the ledger-owner gate, or the single-instance lock.
Momentum and sub-1h entries are not in the library.

Tick-built bars from the data dir (read-only; no bars1m cache written).

- regime@60m: OOS trades 30, mean 165.3 bps, return 17.18%, max DD 5.53%, buy-and-hold mean 3616.5 bps, promotion not yet (failed: lower_bound_positive)
- meanrev@60m: OOS trades 69, mean -31.3 bps, return -7.08%, max DD 8.16%, buy-and-hold mean 3616.5 bps, promotion not yet (failed: mean_positive, lower_bound_positive, ex_best_2_positive, beats_cash)
- trendhold@60m: OOS trades 118, mean -70.0 bps, return -3.27%, max DD 4.49%, buy-and-hold mean 3616.5 bps, promotion not yet (failed: mean_positive, lower_bound_positive, ex_best_2_positive, beats_cash)
- regime@240m: OOS trades 13, mean 81.1 bps, return 3.13%, max DD 8.00%, buy-and-hold mean 2902.0 bps, promotion not yet (failed: min_trades, lower_bound_positive, ex_best_2_positive)
- meanrev@240m: OOS trades 20, mean 41.1 bps, return 2.69%, max DD 3.68%, buy-and-hold mean 2902.0 bps, promotion not yet (failed: min_trades, lower_bound_positive, ex_best_2_positive)
- trendhold@240m: OOS trades 19, mean 310.0 bps, return 2.37%, max DD 0.65%, buy-and-hold mean 2902.0 bps, promotion not yet (failed: min_trades)

No candidate cleared the promotion bar. Nothing registered.

Order-flow overlays (35 pre-registered configs): no shadow candidate; see reports/orderflow_overlays_2026-10-18T210100Z.md

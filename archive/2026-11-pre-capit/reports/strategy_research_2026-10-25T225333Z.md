# Strategy research 2026-10-25

Paper research only. This run does not change PAPER_TRADING, DRY_RUN,
ALLOW_LIVE_TRADING, the ledger-owner gate, or the single-instance lock.
Momentum and sub-1h entries are not in the library.

Tick-built bars from the data dir (read-only; no bars1m cache written).

- regime@60m: OOS trades 31, mean 159.7 bps, return 16.91%, max DD 4.94%, buy-and-hold mean 3762.6 bps, promotion not yet (failed: lower_bound_positive)
- meanrev@60m: OOS trades 75, mean -40.6 bps, return -9.78%, max DD 10.08%, buy-and-hold mean 3762.6 bps, promotion not yet (failed: mean_positive, lower_bound_positive, ex_best_2_positive, beats_cash)
- trendhold@60m: OOS trades 121, mean -69.1 bps, return -3.31%, max DD 4.66%, buy-and-hold mean 3762.6 bps, promotion not yet (failed: mean_positive, lower_bound_positive, ex_best_2_positive, beats_cash)
- regime@240m: OOS trades 13, mean 95.4 bps, return 4.62%, max DD 6.66%, buy-and-hold mean 3012.8 bps, promotion not yet (failed: min_trades, lower_bound_positive, ex_best_2_positive)
- meanrev@240m: OOS trades 20, mean 51.1 bps, return 3.38%, max DD 3.68%, buy-and-hold mean 3012.8 bps, promotion not yet (failed: min_trades, lower_bound_positive)
- trendhold@240m: OOS trades 21, mean 260.8 bps, return 2.20%, max DD 0.65%, buy-and-hold mean 3012.8 bps, promotion not yet (failed: min_trades, lower_bound_positive)

No candidate cleared the promotion bar. Nothing registered.

Order-flow overlays (35 pre-registered configs): no shadow candidate; see reports/orderflow_overlays_2026-10-25T233748Z.md

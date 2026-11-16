# MAYO V1 — Strategy Lock and Data Migration

Updated 2026-11-16. **This is a proposed, reviewable migration.** The operational machine and its `.env` are not changed by a GitHub PR.

## Mandate
MAYO is a **regular spot-market trading research system**, not a meme-token bot. The active paper strategy is `regime_trend` on **60-minute closed candles** for BTC/USD, ETH/USD, SOL/USD. Use the existing ADX, trend, regime, daily filter, volatility and protective-exit modules. The edge is **not proven**: prior candidate results were sensitive to outliers. All live order permissions stay blocked.

Only the 60m regime-trend strategy may enter the V1 paper-book. 4h mean-reversion, trend-hold, futures and retired 15m strategies remain research-only, disabled by default. Do not enable additional sleeves without a versioned strategy review.

## Collection contract
1. Run `bash scripts/collect_regime_data.sh` on the intended collection host. This calls the **public** Kraken OHLC endpoint at 60m/240m/1440m for BTC/ETH/SOL, caches CSVs, and writes `data/regime_trend_v1_collection.json` with UTC collection time, source, symbols, periods and row counts. No keys or private endpoints.
2. For longer history, run `bash scripts/collect_regime_data.sh --with-trades`; this calls the existing paged public trade backfill, which may take substantial time. The collector must record gaps and must not silently interpolate missing 60m bars.
3. For continuous capture, run the already-existing `scripts/tick_collector.py` and monitor its heartbeat. Neither this PR nor a one-off OHLC fetch starts a persistent service.
4. Keep **raw observations**, **strategy-generated features**, **paper trades**, and **research results** separate. Never count a replay as realized customer revenue or live order evidence.
5. Kraken's public OHLC endpoint supplies a rolling ~720 bars per timeframe. A single snapshot is **not** six months of hourly evidence. Check actual time coverage and continue accumulating validated ticks or historical trades.

## Old records: safe separation
- This PR removes the **tracked** historical research files in `data/`, `reports/`, and `backtest_sweep.json`, plus PUMP-specific handoffs. All remain recoverable from earlier Git commits; this does **not** rewrite Git history.
- DO NOT erase local `logs/`, execution databases, paper positions, nonces, idempotency records or old tick/quote captures while any process may be running. Existing real holdings must be reconciled, not forgotten.
- For a clean V1 paper run: stop supervised paper processes; export and archive prior logs/data privately; confirm no open paper positions and no unresolved exchange orders; clear only the obsolete **paper-only** state with the coder; restart the paper launcher; check the first data heartbeat and gate report.
- Previously generated learner priors must **not** be treated as V1 evidence. The V1 learner uses distinct paths; `data/regime_trend_v1_priors.json` does not exist until rebuilt and verified from new evidence.
- The migration intentionally leaves existing historical Python strategy modules available for code review and backtest-only inspection. It does not silently strip order safety code.

## Acceptance / release gates
- Confirm default and `.env.example` agree: `regime_trend` 60m; BTC/ETH/SOL basket; all live locks closed; no retired sleeve activated.
- Confirm no PUMP pair in active symbol defaults or public history collector.
- `ruff check .` and `pytest -q` must pass **on the proposed branch** before merge.
- Collect a fresh dataset with source/time/rows/gaps. Generate cost-aware walk-forward reports **without using old promotional results**.
- Require >=30 out-of-sample closed trades, **profit factor >=1.10 after fees and slippage**, minimum edge stability with best winners removed, and acceptable maximum drawdown. Data leakage, small samples or differing engine replay → NO PROMOTION.
- Review active coder branches before merge. Do not automatically merge, restart the box/Mac, change keys or enable live execution.

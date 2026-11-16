# Archive: everything before the CAPIT-24 reset (2026-11-15)

On 15 Nov 2026 the bot was cut down to **one** paper strategy: frozen CAPIT-24 crash-buy on
VET / HBAR / ALGO / XLM, Binance.US fees (see `../../paper/capit_binanceus/README.md`).
Everything below was moved here with `git mv` (history is kept: `git log --follow <file>`).
Nothing was deleted. Runtime logs and data under `logs/` and `data/` were **not** touched.

None of this code is imported by the active runner or by the test suite (`testpaths = ["tests"]`,
so `archive/**/tests` is not collected). Archived code may not run as-is; it is kept for reference.

## What and why

| Archived | What it was | Why it is retired | Evidence |
|---|---|---|---|
| `src/dublin_bot/intraday_sleeve.py`, `research/intraday/` | Intraday reset / "fewer, longer" / mtf_synth sleeves on 15m-1h bars | Failed the bar after fees on Kraken and Binance.US (e.g. mtf_synth v2 OOS PF 0.73, 1/4 folds; v1 PF 0.37, 0/4) | `reports/intraday_reset_2026-10-24.md`, `reports/intraday_fewer_longer_2026-10-24.md`, `reports/intraday_binanceus_2026-10-25.md` |
| `src/dublin_bot/meanrev_sleeve.py`, `strategies/meanrev4h_strategy.py`, `docs/MEANREV_SLEEVE.md`, `scripts/study_meanrev.py` | 4h mean-reversion sleeve (post-only limits) | No edge after Kraken fees; MR5 on the low-price set also failed (no coin p<0.01) | `reports/strategy_research_*.md`, `../../reports/low_price_coins_study_2026-11-15.md` §5.4 |
| `src/dublin_bot/trendhold_sleeve.py`, `strategies/trendhold_strategy.py`, `docs/TRENDHOLD.md`, `scripts/study_d1_trendhold.py` | 4h trend-hold sleeve with D1 filter | Never cleared the promotion bar in paper | `reports/strategy_research_*.md` |
| `src/dublin_bot/volbrk_sleeve.py` | Volatility-breakout sleeve | Same — no fee-surviving edge | `reports/strategy_research_*.md` |
| `src/dublin_bot/alt_regime_sleeve.py`, `alt_regime_shadow.py` | Alt-coin regime rotation sleeve + shadow | Shadow never earned promotion | `reports/strategy_research_*.md` |
| `research/daily_trend/`, `reports/daily_trend_quickcheck_2026-11-15.md` | Zarattini/Pagani/Barbon daily Donchian ensemble on BTC/ETH/SOL for a $100 Kraken book | NO-GO on Kraken fees/minimums | `reports/daily_trend_quickcheck_2026-11-15.md` |
| `research/signal_frequency_study.py`, `SIGNAL_FREQUENCY_REPORT.md` | Signal-count study for the old sleeves | Superseded | — |
| `scripts/paper_trader_loop.py`, `scripts/run_paper_mac.sh` | The old multi-sleeve main paper loop (regime_trend + meanrev + trendhold + volbrk + alt) and its Mac wrapper | Replaced by `scripts/capit_paper.py`. `deploy/box/run_paper_supervised.sh` now refuses to start (file missing) instead of restart-looping | — |
| `reports/orderflow_*`, `reports/strategy_research_*`, `reports/perp_study_2026-10-10.md`, `reports/futures_short_2026-10-03.json`, `reports/trailing_tp_2026-10-03.json` | Weekly order-flow / walk-forward research outputs, perp and futures-short studies, trailing-TP study | Results for retired strategies (order flow ranked weakest; futures short: 4 trades at −164 bps; trailing TP did not help OOS) | the files themselves |
| `docs/STRATEGY_SWITCH.md`, `backtest_sweep.json` | Old strategy-switch notes and sweep output | Superseded | — |
| `tests/test_*` (6 files + 3 extracted modules) | Tests for the code above | Moved with the code; not collected | — |

## Kept in place on purpose (shared infra, still tested)

- Exchange clients and pipeline: `kraken_gateway.py`, `binance_gateway.py`, `pipeline/` (Binance.US public feed).
- Safety: `config.py` locks (`PAPER_TRADING=true`, `DRY_RUN=true`, `ALLOW_LIVE_TRADING=false`, futures/margin live flags off), `venue_locks.py`, `emergency.py`, `idempotency.py`, audit log, `tests/test_safety_locks.py`.
- `promotion.py` (report-only bar), `scorecard.py`, `watchdog.py`, `exit_watcher.py`, dashboard.
- The gated `engine.py` and its built-in strategies (`strategy.py`: trend breakout / mean reversion, `strategies/`: breakout, regime, sr_flip, pattern, rotation) and the ADX sit-out / fee min-edge gates in `indicators.py`. These are **dormant**: nothing runs them now. They stay because the engine, dashboard, CLI and ~600 safety/infra tests depend on them; ripping them out is a separate, riskier refactor.
- `research/edge2/` (source of the frozen CAPIT-24 rule; `capit_shadow.py` is executed by the running shadow loop) and `research/edge3/` (R2 / OI-flush variants tracked by that shadow; an OI +5 min re-check is still pending).
- `shadow/capit24/` — the standalone CAPIT-24 signal shadow on the old 12-coin set. Still running; research only.

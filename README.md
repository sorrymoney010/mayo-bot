# MAYO — Regular Spot Trading Research

**Current mandate:** research and paper-test one-hour `regime_trend` on **BTC/USD, ETH/USD, and SOL/USD**. This repository is **not** a meme-coin, pump.fun, copy-trading or token-sniping bot.

**LIVE TRADING IS LOCKED:** `PAPER_TRADING=true`, `DRY_RUN=true`, `ALLOW_LIVE_TRADING=false`. No merge, strategy verdict, data run, or PR authorizes real orders.

## Active V1 profile
- Regime-trend on closed **60-minute** bars using ADX/trend/volatility filters and chandelier-style exits; conservative risk and stop controls remain in force.
- Universe restricted to BTC/ETH/SOL regular spot markets. Other strategies remain research code, but **mean reversion, trend-hold and futures sleeves default off**.
- `scripts/run_paper_mac.sh` forces the paper locks and the strategy profile; `.env.example` and code defaults are aligned.
- New versioned learner state: `logs/regime_trend_v1_learner.json`. Historical paper-ledger files on a machine are **not** automatically deleted or migrated.

## Collect fresh public-market evidence
```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
bash scripts/collect_regime_data.sh
# Optional deeper historical reconstruction; public REST paging can be slow:
bash scripts/collect_regime_data.sh --with-trades
```
The existing `scripts/tick_collector.py` is the continuous public Kraken tick/quote collector for these three pairs, with gap detection. The script above fetches fresh OHLC snapshots and writes a timestamped collection manifest. Local market data is excluded from git. **Running these commands is required to actually collect data; adding code alone does not run a collector.**

## Verify before any strategy decision
```bash
ruff check .
pytest -q
```
Document source windows, gap coverage, signal implementation, fee/slippage assumptions and out-of-sample results. Minimum research criteria: >=30 OOS closed trades, profit factor >=1.10 **after costs**, and robustness checks. Passing a bar does *not* permit live trading.

- [V1 strategy lock and migration procedure](docs/ACTIVE_STRATEGY.md)
- [Safety model](SAFETY.md)
- [Operational runbook](RUNBOOK.md)

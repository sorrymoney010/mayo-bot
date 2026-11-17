# Coder handoff — 2026-11-17

Owner: Cheef. Prepared by Claude after a live-readiness audit of `main`
(`3bc3786`, "Merge Dublin- into mayo-bot", 2026-10-05).

**Safety locks are unchanged:** `PAPER_TRADING=true`, `DRY_RUN=true`,
`ALLOW_LIVE_TRADING=false`. Nothing here authorizes live trading.

## TL;DR

1. The engine is solid: 601 tests pass, ruff clean, exchange-side stops,
   hash-chained audit log, circuit breakers.
2. **On Kraken there is no edge.** Kraken's new fee tiers (2026-07-09) are
   0.40% maker / 0.80% taker at Tier 1. The default live strategy
   (`regime_trend@1h`) re-scores to about −9 bps per trade.
3. **On Binance.US the same stored results look strongly positive**, because
   Binance.US spot is 0% maker / 0.02% taker (2026-04-22). Owner says the new
   build targets Binance.US. This is the main lead, and it is unproven.
4. The Binance.US strategy itself ("Capit 24 L" on VET/HBAR/ALGO/XLM) is
   **not in this repo**. We need its code before anything else.

## What's on this branch (`fix/live-readiness-audit`)

| Change | Files | Why |
|---|---|---|
| Unit-safe epoch math (`.as_unit("ns").asi8`) | `telemetry.py`, `daily_filter.py`, `shadow.py`, `pipeline/{bars,history,source}.py` | `asi8 // 10**9` assumes ns. On pandas 3 (µs default) bars vanish and the D1 filter blocks every entry. Fixed 23 failing tests. |
| Safe defaults restored | `config.py` | `max_orders_per_day` 0→3 (0 = no cap), `universe_mode` all_usd→basket, `timeframe_minutes` 15→60 (sub-1h is retired), `strategy_equity_usd` 100→25 |
| Kraken fee model 40/25 → 80/40 bps | `config.py`, `run_paper_mac.sh`, all `scripts/study_*`, `backtest_*`, `strategy_research.py` | Matches Kraken Tier 1 since 2026-07-09 (`.env.example` already had it) |
| Stale test expectations | `test_config.py`, `test_ops_safety.py`, `test_advanced_orders.py`, `test_pipeline.py` | Encoded retired defaults |
| `scripts/rescore_fees.py` | new | Re-scores stored OOS results under any fee schedule |
| `docs/AUDIT_2026-11-17.md` | new | Full audit write-up |

## Venue re-score (stored OOS results, net bps per trade)

From `python scripts/rescore_fees.py` on `data/walkforward_results.json`
(generated 2026-09-26; Kraken prices for BTC/ETH/SOL/PUMP; 90 days at 15m/1h,
120 days at 4h). Ranges on `*_mk` = maker vs taker exit.

| Strategy | OOS n | Gross | Kraken T1 | Binance.US |
|---|---|---|---|---|
| regime @1h | 49 | +161 | −9 | **+147** |
| regime, limit entry @1h | 51 | +105 | −20..+25 | **+98..+105** |
| mean-reversion, limit entry @4h | 27 | +182 | +57..+102 | **+175..+182** |
| regime, limit entry @4h | 24 | +159 | +34..+79 | **+152..+159** |
| regime @4h | 26 | +98 | −72 | +84 |
| breakout @15m | 134 | +87 | −83 | +73 |
| all 1d families | 15–104 | negative | negative | negative |

**Do not treat the Binance.US column as an edge yet.**
- Prices are Kraken's, not Binance.US's. Binance.US alt books are thin, so
  real spread and slippage may be far above 5 bps. Re-run with `--slip 15`
  and `--slip 30` as a stress check.
- Sample sizes are small (24–51 trades on the 1h/4h winners) and come from a
  single, mostly rising window. Trend-hold @4h returned +16% while
  buy-and-hold BTC/ETH/SOL returned +44% over the same OOS period.
- Binance.US says fees are "subject to change". Read the real rate per pair
  from the account; don't hard-code it.

## Binance.US status

- `src/dublin_bot/binance_gateway.py` exists (keyless/dry-run tested) and
  already defaults to `https://api.binance.us`. But `resolve_symbol` forces
  every `USD`/`USDC` quote to `USDT` (`BTC/USD` → `BTCUSDT`), while the
  research tape on 2026-11-16 uses Binance.US USD pairs (`BTCUSD`, `VETUSD`, …).
  Pick one quote per market deliberately and read tick/lot/min-notional and
  fees from the exchange.
- `tests/test_binance_gateway.py` hits the live Binance API from the unit
  suite. Mock it or mark it as a network test.
- Research branches (not merged), all from 2026-11-16:
  - `research/binance-four-coin-fill-audit-20261116`: limit-fill checker plus
    `docs/BINANCE_FOUR_COIN_EVIDENCE.md` and
    `docs/INDEPENDENT_EDGE_RESEARCH_20261116.md`
  - `research/live-tape-bridge-20261116`: read-only Binance.US market tape
    and Trend6 diagnostic
  - `chore/lock-regime-trend-20261116`: large cleanup (−16.5k lines) plus
    strategy lock; needs review before merging with this branch
- Per those docs: "Capit 24 L" was reported at PF 2.47 over 58 trades, picked
  from ~530 variants with coins chosen after seeing results. Its last 5 paper
  signals never filled (limit at 0.995 × signal close). None of it has been
  reproduced.
- The owner also mentioned a branch `claude/youthful-keller-p7vt92`. It is not
  in mayo-bot; the repo it lives in is still unknown.

## Work list, in order

1. **Get the Capit 24 L / Binance.US runner code into this repo** on its own
   branch, plus where and how it's currently running.
2. **Confirm nothing is trading live.** `docs/BUILD_STATUS.md` says a
   coordinator on older code was live on Kraken; the Binance runner's state
   is also unknown.
3. **Finish the Binance.US gateway**: fix the USD/USDT quote mapping, read
   real tick/lot/min-notional and fees from the account, and make fees
   per-venue in config instead of a single `paper_*_fee_bps`.
4. **Collect Binance.US history** for BTC/ETH/SOL and VET/HBAR/ALGO/XLM; extend
   the tick collector so samples go well past 720 bars.
5. **Re-run the walk-forward on Binance.US prices** with realistic spread and
   slippage. Candidates: regime @1h, limit-entry mean-reversion @4h,
   limit-entry regime @4h.
6. **Paper-trade the survivors on Binance.US** until each has ≥30 closed
   forward trades with PF ≥ 1.10 after costs (the repo's own gate).
7. **Close BUILD_STATUS live gates 5–8** for whichever venue goes live.
8. Only then the owner flips the three locks and sets
   `LIVE_RISK_ACKNOWLEDGEMENT=I_ACCEPT_LIVE_TRADING_RISK`.

## Running the tests

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q            # expect 601+ passed; test_binance_gateway needs network
ruff check src tests scripts run_dashboard.py
```

`pyproject.toml` pins `pandas<3`; the timestamp fix makes the code work on
pandas 3 too.

## Sources

- Kraken Pro fee tiers (2026-07-09): https://blog.kraken.com/product/pro/new-kraken-pro-fee-tiers
- Binance.US 0% maker / 0.02% taker (2026-04-22): https://blog.binance.us/zero-fee-trading/
- Zarattini, Pagani & Barbon, *Catching Crypto Trends* (2025), daily
  trend-following tested at 0.10% costs: https://www.cxoadvisory.com/technical-trading/crypto-asset-trend-following-strategies/

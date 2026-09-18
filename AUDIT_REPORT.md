# Dublin Kraken Bot — Audit Report & Remediation (Codex handoff)

Generated: 2026-08-21 (read-only audit → fixes applied)
Auditor: Hermes agent. Mode: READ-ONLY audit first, then build/fix on owner
approval. No live orders were placed; no exchange writes performed.

## 0. Scope & how to reproduce

- Source under review: `src/dublin_bot/*`, `tests/*`, `scripts/backtest*.py`.
- Live config is read from `.env`; working tree had an IN-PROGRESS advanced-order
  branch already present (bracket stops, adaptive-risk logic, no-sweep guard,
  budget cap, bot-lot ledger). **This work was preserved, not clobbered.**

### Verify the build
```bash
cd Dublin-local
source .venv/bin/activate
python -m pytest -q            # 231 passed (was 225; +6 new)
PYTHONPATH=src:. python scripts/backtest_live.py --symbol PUMP/USD --days 200 --oospct 25
```

---

## 1. P0 — Bugs fixed in this pass

### B1. Real Kraken stop-loss (PROTECTIVE STOP) — ALREADY IMPLEMENTED in branch; verified
- `src/dublin_bot/orders.py` builds a Kraken `close[ordertype]=stop-loss` leg
  attached to the parent `AddOrder`. `kraken_gateway.add_order()` submits it.
- `engine._execute_buy()` routes every entry (when `use_bracket=True`) through
  `BracketPlan`, and `_execute()` cancels attached stops on exit
  (`_cancel_attached_bracket`). TP is a separate resting limit (avoids the
  `close[1]` OCO form this tier rejects).
- `config.py`: `use_bracket=True`, `stop_loss_pct=0.04`, `take_profit_pct=0.08`
  are live defaults. This closes the original "no real stop" finding.
- **Residual caveat for owner:** the dashboard monitor loop auto-runs
  `run_once()` every `MONITOR_INTERVAL_SECONDS` (900s). When live flags are set,
  this places real bracket orders with no extra runtime confirmation. The stop
  lives on Kraken, so it protects even if the bot is down — good. But confirm
  the `stop_loss_pct` (4%) is acceptable for the coin volatility (PUMP/USD can
  move >4% in one 15m bar; a gap could fill the stop well below it).

### B2. Adaptive risk now persists across cycles — FIXED
- Root cause (original audit): `RiskManager.update_scale_from_trade` existed
  but `win_streak/loss_streak` were never written, AND the dashboard rebuilds a
  fresh `TradingEngine`/`RiskManager` every cycle, so the in-memory scale reset
  to 1.0 each tick → feature was a permanent no-op.
- Fixes applied:
  - `state.py`: `SessionState` risk fields (`risk_scale`, `win_streak`,
    `loss_streak`) are now loaded/saved by `StateStore`, so a brand-new
    `RiskManager` per cycle restores the adaptation.
  - `risk.py`: `effective_risk_per_trade(state)` reads `state.risk_scale`
    (source of truth); `update_scale_from_trade` applies the delta onto the
    persisted scale and writes it back.
- Tests: `tests/test_adaptive_risk_persistence.py` (3 cases) confirm
  persistence + loss-streak tightening + adaptive-off ignores scale.

### B3. Closed-trade P&L cursor + double-count — FIXED
- `kraken_gateway.closed_trade_pnl` previously returned
  `latest * 1_000_000_000` (treating the cursor as nanoseconds) while Kraken's
  `TradesHistory` `start`/`time` are **seconds**. After the first run the cursor
  became absurdly large, so every subsequent `since` returned nothing and live
  self-learning silently stopped ingesting P&L.
- Fixes applied:
  - `kraken_gateway.closed_trade_pnl` returns `(rows, latest)` in **seconds**,
    one row per trade (txid, symbol, pnl, ts) — not a pre-aggregated sum.
  - `learner.sync_from_exchange` now de-duplicates by Kraken `txid`
    (`self._seen`), so a re-fetch after a restart (or wide `since` window)
    never double-counts realized P&L. `seen_txids` are persisted (bounded).
- Tests: `tests/test_closed_trade_cursor.py` (3 cases) cover seconds cursor,
  txid dedup, and the legacy-absurd-cursor case.

---

## 2. P1 — Positive-expectancy evidence (audit finding P1-a) — NOW PROVIDED

The shipped `scripts/backtest.py` only tested `TrendBreakoutStrategy` and
`scripts/compare_strategies.py` used hand-rolled signal functions — neither
tested the production strategy. **New `scripts/backtest_live.py` replays the
actual pipeline** (`build_strategy` + `RiskManager.evaluate` with the real
stop + `FillModel` ask/bid fills w/ spread+slippage+fee) against real Kraken
OHLC, with an in-sample / out-of-sample split.

### Live results (200d, 15m bars, $1000, fee+slippage modeled, real stop)

| Config | Trades | Win% | Expectancy/trade | Net | OOS expectancy | Verdict |
|---|---|---|---|---|---|---|
| momentum (default) / PUMP/USD | 46 | 17% | **+$0.012** | +$0.57 | **−$0.047** | IS marginally +, OOS − |
| **mean_reversion / PUMP/USD** | 7 | 57% | +$0.032 | +$0.23 | (n/a, too few) | + but **tiny sample** |
| **mean_reversion / BTC/USD** | 6 | 0% | **−$0.036** | −$0.22 | (n/a) | **NEGATIVE — do not trade** |

### Interpretation for the owner
- **There is NO robust positive-expectancy evidence for live trading yet.**
  - Sample sizes for mean_reversion are 6–7 trades over 200d — statistically
    meaningless (a single trade flips the sign). The strategy barely trades
    (RSI≤38 AND below slow EMA is rare on 15m).
  - The default `momentum` config is marginally positive in-sample but
    **negative out-of-sample** on PUMP/USD.
  - mean_reversion is **negative on BTC/USD** — yet BTC/USD is in the live
    `universe_allowlist`. The allowlist claims "positive backtested
    expectancy" but the only coin with even weak IS positivity is PUMP/USD,
    and that is on a 7-trade sample.
- **Decision:** Do NOT enable live trading on the current evidence. Before
  going live, either (a) gather ≥100 trades per coin via a longer window /
  higher-frequency bars, or (b) widen the entry (already loosened:
  `rsi_oversold=38`) and re-backtest, or (c) accept that this is a
  near-breakeven strategy and size it as such.
- The backtester is the durable artifact: run it across multiple windows and
  coins before any live decision. It charges realistic fills, unlike the
  prior harnesses.

---

## 3. Remaining issues NOT fixed (out of scope / need owner decision)

- **E1 (residual):** `stop_loss_pct=4%` may be too tight for PUMP/USD; gap
  risk. Consider a volatility-scaled stop (ATR-based) instead of fixed %.
- **E2 (dead config):** `auto_cheaper_symbol` / `fallback_symbols`
  (`config.py`) are never read; the autonomous selector ignores them. The bot
  simply excludes unaffordable coins rather than "falling back" to a cheaper
  one. Either wire it or remove it. (Low risk, cosmetic.)
- **E3 (regime inert):** `learner.last_regime` is always "unknown" — no regime
  detector exists. `bias()`/`regime_penalty()` operate on a single regime, so
  per-regime expectancy never varies. Coin selection bias is effectively flat.
- **E4 (periodic, not event-driven):** execution is one decision per
  `MONITOR_INTERVAL_SECONDS`; no intrabar reaction except the optional WS
  realtime-stop check (`check_realtime_stop`). Fine for 15m bars; not HFT.
- **E5 (tests false-green, from original audit, still present):** `test_repeated_cycles_on_the_same_bar_do_not_duplicate` only asserts the
  duplicate-block branch *inside* `if first.executed:` and otherwise falls back
  to a vacuous `len(confirmed) <= 1`; the default strategy on the uptrend
  fixture never executes, so the real branch is unverified. Recommend a
  crafted oversold fixture that forces a BUY and asserts the second cycle is
  blocked.

---

## 4. Codex checklist (copy into the PR description)

- [x] B1 Real Kraken stop-loss via bracket order — verified implemented
      (`orders.py`, `kraken_gateway.add_order`, `engine._execute_buy`).
- [x] B2 Adaptive risk persists across dashboard cycle rebuilds
      (`state.py` SessionState persistence; `risk.py` reads `state.risk_scale`).
- [x] B3 `closed_trade_pnl` uses seconds cursor + per-trade rows; learner
      de-dupes by txid (`kraken_gateway.py`, `learner.py`).
- [x] P1-a Real-strategy backtest added (`scripts/backtest_live.py`) using the
      actual strategy+risk+fill pipeline against live Kraken history.
- [x] Tests: +6 new (cursor, dedup, adaptive-risk persistence). Full suite 231
      passed.
- [ ] Owner decision: do NOT go live — evidence is not robust (≤7 MR trades;
      momentum OOS negative; BTC/USD MR negative despite being allowlisted).
- [ ] Follow-up (optional): volatility-scaled stop (E1); wire or remove
      dead `fallback_symbols` (E2); add regime detector (E3); strengthen
      duplicate-prevention test (E5).

## 5. Files changed in this pass
- `src/dublin_bot/kraken_gateway.py` — `closed_trade_pnl` seconds cursor + rows.
- `src/dublin_bot/learner.py` — txid dedup, `_seen` set, persisted.
- `src/dublin_bot/state.py` — persist `risk_scale`/`win_streak`/`loss_streak`.
- `src/dublin_bot/risk.py` — `effective_risk_per_trade(state)`; scale delta on
  persisted value.
- `scripts/backtest_live.py` — NEW real-strategy fee-adjusted backtester.
- `tests/test_closed_trade_cursor.py` — NEW.
- `tests/test_adaptive_risk_persistence.py` — NEW.
- (B1 was already present in the working tree; no change needed.)

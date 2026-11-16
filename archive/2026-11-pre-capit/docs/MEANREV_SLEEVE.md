# 4h mean-reversion sleeve (paper only)

_Added 2026-09-26. Everything here is PAPER. Live stays locked
(`PAPER_TRADING=true DRY_RUN=true ALLOW_LIVE_TRADING=false`)._

A second paper sleeve that runs in the same loop (`scripts/paper_trader_loop.py`)
as the primary `regime_trend@60m` sleeve. Code: `src/dublin_bot/meanrev_sleeve.py`,
signal: `src/dublin_bot/strategies/meanrev4h_strategy.py`, shared rules with the
backtester: `backtest_core.meanrev_signals`.

## Why these parameters

Walk-forward (`data/walkforward_report.txt`, `docs/WALKFORWARD.md`, 90 bps taker round
trip, maker 25 bps/side), family `meanrev_mk` at 240m. For BTC, ETH and SOL, **the default spec
was picked in every walk-forward fold** (SOL fold 1 picked the 5% stop variant):

`rsi_os=38, rsi_exit=55, stop=3%, tp=25%, entry=limit, limit_offset=0.1%` (valid 1 bar)

| symbol | OOS trades | OOS net bps/trade |
|---|---:|---:|
| BTC | 7 | +22 |
| ETH | 5 | +243 |
| SOL | 5 | +139 |
| **BTC+ETH+SOL** | **17** | **≈ +121 (trade-weighted)** |
| PUMP (excluded) | 10 | +92 at 4h, but negative for most other PUMP families/timeframes |

The headline +110 bps/27 trades includes PUMP. **17 trades is a tiny sample**: treat
this as an experiment, not a proven edge. At stress costs the ex-best-2 result was ≈ 0.

## Rules in plain words

* **Timeframe:** 4h bars, evaluated only on CLOSED bars.
* **Entry signal:** RSI(14) at or below 38 **and** the close is below the 50-period EMA
  (a washed-out dip).
* **Entry order:** a paper post-only limit buy 0.1% under that bar's close, kept strictly
  below the ask. It lives for one 4h bar. It fills (at the limit, maker fee 25 bps) only if
  the live ticker trades at or through the limit during that bar. If not, it **expires**.
  It is never converted into a market order. One order per signal bar.
* **Exits:**
  * stop: last price 3% or more below the fill → sell at market (taker 40 bps + slippage);
  * take-profit: last price 25% or more above the fill → sell at the TP price (maker);
  * "reverted": on a 4h bar that closed after the fill, RSI ≥ 55 **or** close ≥ EMA50 →
    sell at market (taker).
* **Symbols:** BTC/USD, ETH/USD, SOL/USD (still filtered by `UNIVERSE_ALLOWLIST`).

## Sharing the book with regime_trend (no double-sizing / no collisions)

* One paper book (`logs/paper_portfolio.json`, $500) and one lot ledger
  (`logs/paper_bot_positions.json`).
* `logs/paper_sleeve_owners.json` (`SleeveRegistry`) records which sleeve owns each lot
  and each resting order. A symbol held **or** pending in one sleeve is off-limits to the
  other: no entries, no exits, no stop/TP from the wrong sleeve. Lots with no entry belong
  to the primary engine.
* **Max concurrent positions** (`MAX_CONCURRENT_POSITIONS`, 3) counts every held lot in both
  sleeves plus every resting order.
* Sizing: the shared `RiskManager` (1% risk to the 3% stop, capped at
  `MAX_POSITION_FRACTION` of the book, 25% by default → ≤ $125 on $500), the shared daily-loss /
  drawdown / cooldown / orders-per-day breakers (`logs/session_state.json`), and the
  exposure cap, where resting orders count as exposure and reserve paper cash.
* The fee-aware minimum-edge gate and the spread/liquidity market-quality gate apply to
  entries, the same as for the primary.

## Adaptive learner

Closed trades are recorded in the shared `logs/learner.json` under strategy key
`meanrev_mk@240m` (priors come from the same walk-forward rows). Expectancy and benches are
scored **per strategy key**: bench keys are now stored as `<strategy>::SYM[|regime]`, so a
bench on `regime@60m` BTC does not bench `meanrev_mk@240m` BTC, and vice versa. Older benches
with no scope still apply to the strategy that wrote them.

## Safety

* The sleeve does nothing unless all three locks are on (`paper_trading`, `dry_run`,
  `not allow_live_trading`). The loop logs `meanrev_4h=off` otherwise.
* It never calls an order-submission method. Its gateway is built with
  `allow_order_submission=False`, and tests fail if any submit method is touched.

## Config

| env | default | meaning |
|---|---|---|
| `MEANREV_SLEEVE_ENABLED` | `true` | toggle (paper only either way) |
| `MEANREV_SYMBOLS` | `["BTC/USD","ETH/USD","SOL/USD"]` | JSON list |
| `MEANREV_TIMEFRAME_MINUTES` | 240 | bar size |
| `MEANREV_RSI_ENTRY` / `MEANREV_RSI_EXIT` | 38 / 55 | RSI(14) thresholds |
| `MEANREV_EMA_PERIOD` | 50 | EMA filter / reversion target |
| `MEANREV_STOP_PCT` / `MEANREV_TAKE_PROFIT_PCT` | 0.03 / 0.25 | protective levels from the fill |
| `MEANREV_LIMIT_OFFSET_PCT` | 0.001 | limit below the signal close |
| `MEANREV_LIMIT_VALID_BARS` | 1 | limit lifetime in bars, then it expires |
| `MEANREV_STATE_PATH` | `logs/meanrev_sleeve.json` | pending orders, positions, events |

## Logs

* `CYCLE sleeve=meanrev_4h active=True actions=<event:symbol,...> errors=N | BTC/USD: ... | ...`
  once per loop cycle (events: `limit_placed`, `limit_filled`, `limit_expired`, `exit`).
* `LEARNER key=meanrev_mk@240m ...` at start.
* `logs/last_cycle_meanrev.json`: full result of the last sleeve cycle.

## Known differences from the backtest

* The backtest fills a limit if the next bar's LOW touched it. Paper fills only when a loop
  poll (every ~5 min) sees ask/last at or below the limit, so it will miss some wick fills.
  This is conservative.
* The backtest checks the stop and TP intrabar. Paper checks them at each poll using the last
  price, so gaps between polls fill at the polled price.

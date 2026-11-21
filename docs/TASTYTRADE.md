# tastytrade integration

Two things were added, both **paper / dry-run by default**:

1. **`BROKER=tastytrade`** — a crypto spot gateway that drops into the existing
   engine next to Kraken and Binance (`src/dublin_bot/tastytrade_gateway.py`).
2. **Put credit spread sleeve** — a defined-risk options strategy on SPY / QQQ /
   IWM (`src/dublin_bot/credit_spread.py`), plus a validation harness that runs
   the exact rules on tastytrade's own historical option data
   (`src/dublin_bot/tasty_backtest.py`).

Sources: developer.tastytrade.com (OAuth2, orders, market data, market metrics,
backtesting specs) and tastytrade.com/pricing, read 2026-11-21.

---

## The honest edge summary

### Crypto on tastytrade: execution venue only, not a better edge

* **tastytrade disabled crypto order entry through the API in release
  `20260629`, "until further notice."** Quotes still work. The gateway refuses
  to submit while `TASTYTRADE_CRYPTO_API_ENABLED=false` (the default), even with
  every other lock open. Flip it only after their release notes say it is back.
* **Fees are the bigger problem.** tastytrade charges 0.75% per side on BTC/ETH
  and 1% on other coins (no maker discount). That is a 160-210 bps round trip vs
  90 bps at Kraken taker. Re-costing the existing walk-forward
  (`scripts/tasty_crypto_recost.py` -> `reports/tasty_crypto_recost.txt`):

  | | rows | positive net edge |
  |---|---|---|
  | Kraken taker (90 bps RT) | 112 | 29 |
  | tastytrade (160-210 bps RT) | 112 | 16 |
  | tastytrade, >= 15 OOS trades | — | **1** (ETH 15m breakout, n=17) |

  One survivor on 17 trades is not evidence of an edge. **Keep the crypto
  sleeves on Kraken.** The tastytrade adapter is there so the engine can route
  to it if fees or the API status change.

### Put credit spreads: real premise, thin margin, must be proven

The edge being harvested is the volatility risk premium (index implied vol
tends to exceed later realised vol). Defined-risk spreads cap the crash loss.
But the margin is thin once costs are counted. Per spread, round-trip costs are
$1 x 2 legs open + $0.10 x 4 leg-fills clearing + $0.02 x 2 slippage x 100 =
**$6.40**. With a 50% take-profit and a 2x stop:

| width | typical credit | costs / credit | break-even win rate |
|---|---|---|---|
| 10 | $150 | 4% | ~82% |
| 5 | $75-100 | 6-9% | ~83% |
| 2 | $32 | 20% | 88% |
| 1 | $15 | 43% | 97% |

So the sleeve **rejects any spread whose costs exceed 10% of the credit**
(`OPTIONS_MAX_COST_TO_CREDIT`), which in practice means 5-wide or wider on SPY.
At 5% risk per trade that needs roughly **$8-10k** in the sleeve
(`OPTIONS_EQUITY_USD`, default $10k paper). Smaller accounts get a clear
"risk budget too small" skip instead of a fee-starved trade.

**Nothing here proves the edge yet.** That is what `dublin-bot tasty-backtest`
is for: it runs the rules on tastytrade's backtester (their historical chains),
subtracts our fees and slippage from every trade, splits the history in half,
and only marks a variant promotable if the out-of-sample half has >= 40 trades,
positive expectancy, profit factor >= 1.2, *and* the in-sample half was also
profitable. Run it before trusting a single paper fill.

---

## Setup (live account, sandbox optional)

1. On my.tastytrade.com: **Manage -> My Profile -> API -> OAuth Applications ->
   + New OAuth client**. Scopes: `read` (and `trade` only when you go live).
   Save the client secret (shown once). Then **Manage -> Create Grant** to get
   the refresh token.
2. Add to `.env` (never commit it):

   ```
   TASTYTRADE_ENV=production        # backtester + real market data
   TASTYTRADE_REFRESH_TOKEN=...
   TASTYTRADE_CLIENT_SECRET=...
   TASTYTRADE_ACCOUNT_NUMBER=...     # optional if the login has one account
   OPTIONS_SLEEVE_ENABLED=true
   ```

   `PAPER_TRADING=true`, `DRY_RUN=true`, `ALLOW_LIVE_TRADING=false` stay as they
   are. With those set, production credentials are used **read-only**: quotes,
   chains, IV rank, backtests. No order can form.
3. Optional: `TASTYTRADE_PREVIEW_ORDERS=true` sends each paper order to the
   broker's `/orders/dry-run` endpoint (never executes) so you see real
   buying-power impact and fees in the audit log.

## Commands

```bash
dublin-bot tasty-health                 # auth, accounts, locks, SPY quote
dublin-bot tasty-backtest --symbols SPY # edge validation; exit 5 if nothing passes
dublin-bot tasty-spreads                # one sleeve cycle (09:45-15:45 ET)
dublin-bot tasty-book                   # open spreads + paper stats
BROKER=tastytrade dublin-bot run-once   # crypto engine via tastytrade (paper)
```

Run `tasty-spreads` every 15-30 minutes during market hours (cron / launchd).

## Strategy rules (defaults)

| Rule | Setting | Default |
|---|---|---|
| Underlyings | `OPTIONS_UNDERLYINGS` | SPY, QQQ, IWM |
| Sell only when premium is rich | `OPTIONS_MIN_IV_RANK` | 0.30 |
| Liquidity rating | `OPTIONS_MIN_LIQUIDITY_RATING` | 3 |
| Entry DTE window / target | `OPTIONS_DTE_MIN/TARGET/MAX` | 30 / 45 / 60 |
| Short put delta (from mid-price IV) | `OPTIONS_SHORT_DELTA` ± `OPTIONS_DELTA_TOLERANCE` | 0.20 ± 0.08 |
| Widths tried, widest that fits | `OPTIONS_WIDTH_CANDIDATES` | 10, 5, 2 |
| Max leg bid/ask vs mid | `OPTIONS_MAX_LEG_SPREAD_PCT` | 15% |
| Min credit vs width | `OPTIONS_MIN_CREDIT_RATIO` | 15% |
| Max costs vs credit | `OPTIONS_MAX_COST_TO_CREDIT` | 10% |
| Risk per trade / total open | `OPTIONS_RISK_PER_TRADE` / `OPTIONS_MAX_TOTAL_RISK` | 5% / 25% |
| Max open spreads (one per underlying) | `OPTIONS_MAX_POSITIONS` | 3 |
| Take profit | `OPTIONS_TAKE_PROFIT_PCT` | 50% of credit |
| Stop | `OPTIONS_STOP_LOSS_MULTIPLE` | loss of 2x credit |
| Time exit | `OPTIONS_EXIT_DTE` | 21 DTE |

Limit price is the mid credit, never worse than the natural credit. Paper
fills assume `OPTIONS_SLIPPAGE_PER_SPREAD` ($0.02) worse than the limit.

Delta: tastytrade's REST quotes have no Greeks (those are DXLink-stream only),
so delta is backed out of each put's mid price via implied vol (Black-Scholes).

## Safety model

A real option order needs **all** of: `allow_order_submission` (constructor),
`ALLOW_LIVE_TRADING=true`, `PAPER_TRADING=false`, `DRY_RUN=false`,
`LIVE_RISK_ACKNOWLEDGEMENT=I_ACCEPT_LIVE_TRADING_RISK`,
`ALLOW_OPTIONS_LIVE_ORDERS=true`, `TASTYTRADE_ENV=production` and an account
number. Crypto additionally needs `TASTYTRADE_CRYPTO_API_ENABLED=true`.

Live path, per tastytrade's guidance: broker dry-run first (any error blocks),
every order carries an `external-identifier`, orders are **never**
auto-resubmitted (the API does not deduplicate); after a timeout / 5xx the bot
searches live orders for its identifier and stops if not found. Live opens and
closes stay `pending` until broker positions confirm them, and a single-leg
mismatch is logged as a reconciliation error.

Not built: rolling, early-assignment handling beyond the 21-DTE exit, holiday
calendar (stale quotes fail the market-quality checks), DXLink streaming.

## Pre-live checklist (options)

- [ ] `tasty-backtest` shows a promotable variant for the underlying
- [ ] >= 30 closed paper spreads with profit factor >= 1.2 after fees
- [ ] No reconciliation errors in the audit log
- [ ] Account is approved for spreads (options level 2+) and has the capital
- [ ] OAuth grant has `trade` scope; read SAFETY.md again

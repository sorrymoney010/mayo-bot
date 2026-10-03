# Paper professional features

Everything on this page is **paper**. The three safety locks are unchanged
(`PAPER_TRADING=true`, `DRY_RUN=true`, `ALLOW_LIVE_TRADING=false`). The
single-instance lock and `MAYO_LEDGER_OWNER` are unchanged. Paper mode still
does not call private Kraken endpoints.

Two extra locks exist for venues the paper bot only simulates. Both default
**off**, and either one is ignored unless the three locks above are also
opened and the live-risk acknowledgement is set:

| Lock | Default | What it gates |
|---|---|---|
| `ALLOW_FUTURES_LIVE_ORDERS` | false | `venue_locks.submit_futures_order` |
| `ALLOW_MARGIN_LIVE_ORDERS` | false | `venue_locks.submit_margin_order` |

Live access to Kraken margin and Kraken Futures for a US account depends on
that account's eligibility. This repo does not claim that a given account can
or cannot trade those products.

## Trailing take-profit — default OFF per sleeve

`dublin_bot.trailing` arms a stop once unrealized gain exceeds
`TRAILING_ACTIVATE_ATR` (default 1.5) times the entry ATR, then ratchets it
`TRAILING_ATR_MULT` (default 1.0) ATR behind the best price. State is
`logs/trailing_state.json`, so a restart keeps an armed trail on an open
position.

Flags (all default **false** until a walk-forward says the overlay improves
out-of-sample results after the 40/25/10 bps spot fee model):

* `TRAILING_TP_REGIME`
* `TRAILING_TP_MEANREV`
* `TRAILING_TP_TRENDHOLD`
* `TRAILING_TP_FUTURES`

Promotion rule (`research.improves`): OOS mean net bps higher, OOS return
higher, max drawdown not worse by more than 1 percentage point, and at least
8 OOS trades. Otherwise the sleeve stays off.

```bash
python scripts/fetch_kraken_history.py ohlc --tfs 60 240 1440
python scripts/backtest_trailing.py
```

The report is `reports/trailing_tp_<date>.json`. Entries are unchanged: the
overlay only exits. Sub-1h entries and the momentum family stay refused.

## Fast exits — default ON

`EXIT_WATCHER_ENABLED` (default true) starts a thread inside
`scripts/paper_trader_loop.py` after the ledger-owner check and the
single-instance lock. It builds 1-minute candles from the public trade/ticker
websocket, the tick-collector file, or a public REST poll, and books a paper
stop, trailing stop, or take-profit when price crosses. Strategy entries and
signal exits (chandelier, RSI, EMA) stay on their own timeframe.

The sell reloads the paper book under a file lock
(`PaperPortfolio.try_record_sell`). A second close of the same lot is a no-op.
`scripts/exit_watcher.py` is a one-shot pass that refuses without
`MAYO_LEDGER_OWNER=1` and the instance lock, so it cannot run beside the loop.

## Futures short sleeve — default OFF

`FUTURES_SLEEVE_ENABLED` (default false). When on, and only while the three
safety locks are still engaged, the sleeve paper-trades Kraken perpetuals
`PF_XBTUSD`, `PF_ETHUSD`, `PF_SOLUSD` using **public** mark, funding rate and
open interest (`dublin_bot.futures_public`). No API key.

* Short only when the daily filter is known and bearish (the mirror of the
  spot risk-on rule) and 4h (or configured ≥1h) EMAs agree: close below the
  slow EMA and the fast EMA below the slow EMA.
* Leverage is hard-capped at **2x** (`futures_leverage` cannot be set above 2;
  the code clamps as well). Default configured leverage is 1x.
* Isolated liquidation is modeled. A cross liquidates the paper position.
* Funding is accrued from the public hourly rate (positive rate pays the short).
  The ticker `fundingRate` is quote currency per base unit; the sleeve divides
  it by the mark (`funding_rate_relative`) before applying it to USD notional.
  Historical studies use `relativeFundingRate` (`--fetch-funding`).
* Fees are Kraken Futures' published base tier: **2 bps maker / 5 bps taker**
  ([fee schedule](https://support.kraken.com/articles/360048917612-fee-schedule)).
  Spot fills stay on 40/25/10.
* Notional counts toward the shared max-3 positions and the exposure cap.

The sleeve stays off unless `scripts/backtest_futures_short.py` clears the
existing promotion bar out of sample (30 trades, mean and lower bound positive,
positive without the best two trades, after fees). Numbers land in
`reports/futures_short_<date>.json`.

```bash
python scripts/fetch_kraken_history.py ohlc --tfs 60 240 1440
python scripts/backtest_futures_short.py --fetch-funding
```

`--fetch-funding` writes `data/futures_funding_<PERP>.csv` (`time,rate` hourly
relative rates from the public historical-funding-rates endpoint). Without
that file the study uses zero funding and says so.

## Study on 2026-10-03 (public OHLC, not a multi-year sample)

Kraken's public OHLC endpoint returns about 720 bars: 30 days at 1h, 120 days
at 4h, 719 days of daily bars for the trend filter. Reports:

* `reports/trailing_tp_2026-10-03.json`
* `reports/futures_short_2026-10-03.json`
* `reports/strategy_research_2026-10-03.md`

Trailing take-profit stayed **off** for every sleeve. Pooled out-of-sample,
after 40/25/10 bps:

| Sleeve | Baseline trades / mean bps / return / max DD | Best trail overlay |
|---|---|---|
| regime @ 1h | 8 / +208.2 bps / +5.65% / 0.91% | 2.0/1.5 ATR: 14 / −9.9 bps / −0.49% / 3.18% |
| meanrev @ 4h | 14 / +94.6 bps / +4.47% / 1.24% | 1.5/1.0 ATR: 15 / +92.6 bps / +4.71% / 0.48% |
| trendhold @ 4h | 19 / +347.8 bps / +2.66% / 0.65% | 2.0/1.5 ATR: 46 / +45.1 bps / +0.83% / 0.91% |

Mean-reversion's tightest overlay raised return and cut drawdown, but mean bps
fell, so `improves()` did not promote it. Buy-and-hold on the same windows
was about +978 bps (1h) and +4406 bps (4h).

The futures short stayed **off**. With published 5 bps taker fees and cached
relative funding: 1h produced **0** shorts (the 30-day window stayed daily
risk-on). 4h produced 4 out-of-sample trades, all losers, mean **−164.4 bps**,
return −1.63% at 1x and −3.25% at 2x, max drawdown equal to that loss. The
promotion bar failed on every check. Buy-and-hold on that 4h window was about
+4406 bps.

The research run registered nothing. No family cleared 30 trades with a
positive mean, a positive lower bound, and a positive mean without the best
two trades. The only family with 30+ out-of-sample trades was trend-hold at
1h: 40 trades, mean **−66.8 bps**, return −1.07%, max DD 2.18%, versus
buy-and-hold about +978 bps.

## Spot margin — modeled, not the short sleeve

`dublin_bot.margin_paper` prices Kraken's published margin band (top of range):
BTC opening and rollover 0.02% per 4 hours, ETH/SOL 0.04%. Rates are dynamic
live and locked at order time; paper uses the top of the band so borrow cost
is not optimistic.

Short **spot** is not a sleeve. Borrow availability and margin level are
private endpoints, which paper mode must not call. The perpetual sleeve is the
short: public mark, funding and open interest, no coin borrow. A live margin
order still has to pass `ALLOW_MARGIN_LIVE_ORDERS` and every existing lock.

## Strategy research — weekly, shadow only

```bash
python scripts/fetch_kraken_history.py ohlc --tfs 60 240 1440
python scripts/strategy_research.py --tfs 60 240 --register
```

The library is regime, 4h-style mean-reversion and trend-hold parameter
variants on **1h and 4h only**. Momentum and sub-1h entries are not searched.
Each family is walk-forwarded with the spot fee model. A candidate is
registered only if it clears the promotion bar in `dublin_bot.promotion`.
Registration writes `logs/shadow_sleeves.json` (gitignored runtime state). The
paper loop logs those signals to `logs/shadow_signals.jsonl` and does not
fill them. The dated write-up is `reports/strategy_research_<date>.md`.

Nothing in that script writes `PAPER_TRADING`, `DRY_RUN`, `ALLOW_LIVE_TRADING`,
or the new venue locks.

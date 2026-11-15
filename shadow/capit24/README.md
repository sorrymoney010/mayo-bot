# CAPIT-24 shadow signal log

**Signal log only.** It places no orders, uses no keys and reads public data only. It is a standalone process and does not touch `paper_trader_loop.py`, its lock, its book or any trading flag. Approved by the user on 2026-11-14 ("A and C").

## Rule (frozen; see reports/edge_search_2026-11-14.md)
- **Bars:** each closed 1h bar for BTC ETH SOL XRP DOGE ADA AVAX LINK LTC BNB DOT TRX on Binance.US.
- **Price-drop condition:** `z24 = 24h log return / (stdev of the previous 720 1h log returns * sqrt(24)) <= -2.5`.
- **Volume condition:** `24h quote volume >= 2 x (mean hourly quote volume over the 720h ending 24h earlier * 24)`.
- **Trade:** a hypothetical buy at the next 1h open, sold 48h after entry at that bar's open.
- **Limits:** at most 2 concurrent positions, one per coin. When signals tie, the most negative z goes first.
- **Sizing:** each slot is 50% of a notional $466.81 ($233.405), non-compounded. Cost is 10 bps round trip (Binance.US taker 2 bps + 3 bps slippage per side).
- **Seed:** 2026-11-14 00:00 UTC. The Oct 1–Nov 13 holdout trades do **not** count toward the 30.

## Data notes
- **Source:** Binance.US public REST. Price comes from `<COIN>USDT`; volume is the sum of the `USDT` and `USD` pairs' quote volume.
- **Fallback:** Binance global REST is geo-blocked (HTTP 451) from this box, so there is no live global fallback. If Binance.US fails, the cycle aborts with no partial decisions and catches up on the next run.
- **Venue differences:** Binance.US volume is much thinner than Binance global, where the backtest was run. DOT, TRX and AVAX trade only tens of thousands of dollars a day. Signals therefore differ somewhat from the global-data backtest. Replaying Oct 1–Nov 14 on Binance.US data gave 3 closed trades (BNB, BTC, ETH; net −24 bps total), while global data gave BNB and LINK.

## Files
- `signals.jsonl`: every signal, with `taken` or `skip_reason` (max_pos / coin_already_held).
- `trades.jsonl`: one line per closed hypothetical trade (gross/net bps, $ P&L).
- `status.json`: last run, open hypothetical positions, closed count, win rate, PF, net bps and equity.
- `state.json`: the cursor (last fully decided signal bar) plus open positions.
- `run.log`: loop output.
- **Catch-up:** each run refetches about 1,100h of candles and replays every undecided bar after the cursor, so pauses lose nothing. IDs are de-duplicated, so nothing is logged twice.

## Run
- Start: `/workspace/mayo-bot/shadow/capit24/start.sh`. It refuses to start a duplicate and runs a cycle every 15 min under `env -i`.
- Stop: `kill $(cat /workspace/mayo-bot/shadow/capit24/loop.pid)`.
- One manual cycle: `env -i PATH=/usr/bin:/bin HOME=/home/box python3 /workspace/mayo-bot/research/edge2/capit_shadow.py`.

## Pass / kill rules (from the report)
- **Promotion review at ≥30 forward closed trades.** All of these must hold:
  - PF ≥ 1.10 after Binance.US costs (10 bps round trip)
  - Mean net per trade > 0
  - Max drawdown no worse than −15% at 50% slots
  - Beats buy-and-hold of the equal-weight 12-coin basket over the same period
- **Kill early:** if the first 15 forward trades have a net mean below −100 bps.
- **Kraken:** costs (80 bps maker / 170 bps taker round trip) are reported for reference only. The rule is not viable at Kraken taker fees.
- Even on a pass, going to paper or live execution needs the user's explicit OK. No flags change automatically.

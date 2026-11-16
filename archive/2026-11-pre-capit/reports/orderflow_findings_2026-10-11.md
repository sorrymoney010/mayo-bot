# Order-flow study — findings (2026-10-11)

Data: Kraken spot ticks BTC/ETH/SOL, longest contiguous run 2026-06-04 21:00 .. 2026-10-04 14:00 UTC
(121.7 days; an unfilled 115 h outage hole Oct 4 13:00 -> Oct 9 08:00 UTC splits the archive).
OOS = walk-forward folds 1-4 after a 210-bar warm-up: 90 days at 1h, 69 days at 4h.
Costs 40 bps taker + 10 slip per side, 25 maker; stress 80+10. 35 pre-registered configs, one run,
fixed thresholds (no threshold search). Full tables: `orderflow_exits_2026-10-11.md`.

## Headline
No order-flow overlay improved any sleeve out of sample in a way that clears the gates.
Nothing is promoted to shadow. The weekly loop will keep re-testing automatically.

| sleeve (baseline OOS) | best overlay | delta | why it does not count |
|---|---|---|---|
| regime@60m (30 tr, +178 bps, median -136, 33% win, +18.4% ret, DD 5.6%) | of_no_dist (skip when large orders net-sell 4h) | +11 bps, 29 tr | removes 1 trade; better in 1 of 4 folds |
| meanrev_mk@240m (14 tr, +95 bps, 79% win, +4.5%, DD 1.2%) | close-based stop (x_closestop_ctrl / x_shakeout_hold identical) | +29 bps, DD 0.48% | 14 trades; the gain is from closing-basis stops, not order flow (the absorption logic never changed a decision); 1 of 4 folds |
| trendhold@240m (19 tr, +363 bps, 32% win, +2.8%, DD 0.65%) | none | every overlay lowered mean | trend P&L lives in the right tail |

Buy-and-hold over the same OOS window (one round trip after fees): +44% (1h window), +41% (4h window),
max DD ~10% / ~8.5%. The sleeves size at 1% risk / 40% notional cap, so their returns are not
leverage-comparable to B&H; they win on drawdown, not on return, in this bull sample.

## What hurt (consistently)
* Sell-into-strength exits: buy-climax exit regime -100 bps (178 -> 78), trendhold -216 bps; euphoria
  exit regime -153, trendhold -285. Half scale-outs hurt less but still hurt.
* Partial take-profits (50% at +2 / +4 ATR): regime -117..-122 bps, trendhold -198..-205;
  meanrev +19 bps at 2 ATR (14 trades, 2/4 folds; noise-level).
* "Prefer entries after absorption" (of_abs_pref): removes all regime breakouts (breakouts do not
  follow absorption), trendhold -476 bps.

## Why (descriptive event study, not used for selection)
* Flow features barely rank forward returns: Spearman IC |ic| < 0.05 almost everywhere and the
  sign flips between the first and second half (e.g. lnet_z_240 vs 4h fwd: -0.014/-0.028/-0.005
  then 0.012/-0.118/-0.036).
* "Euphoria" (price >= 2.5 sd above 24h VWAP while large orders net-sell) was followed by +132..+276 bps
  over 24h vs +26..+50 unconditional: in this sample big sellers into strength did NOT mark tops;
  momentum continued. That is exactly why selling into it lost money.
* Selling climaxes bounce a little (+31..+34 bps over 4h on BTC/SOL, -3 on ETH) and absorbed dips add
  ~+5 bps vs unconditional at 4h: real but far smaller than a 90 bps round trip, so useful at most as
  exit-delay logic; stop-outs are too rare (2/30 regime, 1/14 meanrev) to test it.
* Caveat: one bull-trending 4-month sample; climax/euphoria counts are small (17-77 per coin).

## Side findings
* The weekly research was silently broken on tick data: tick-built bars used the LATEST contiguous
  run, which after the Oct 4-9 outage is 2.4 days -> 0 trades in every family. Fixed in this branch
  (latest run if >= 60 days, else longest).
* The Oct 4-9 tick hole is unfilled in the live data dir. Forward shadow scoring needs contiguous ticks
  (>= 210 h for regime@60m, 10 days / 18 days for the 4h sleeves), so a backfill of that hole on the
  box would help; this branch does not touch the live data.
* With the user's cost model the fixed live regime@60m spec clears the bar marginally
  (30 OOS trades, +178 bps, 90% LB +1 bps, ex-best-2 +18 bps), median still negative.

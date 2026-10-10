# Kraken Futures perp strategy study, 2026-10-10

Paper research only. Ran in the copy at /workspace/mayo-futures (git archive of mayo-bot 6aae712) with its own venv (/workspace/mayo-futures-runtime/pyenv). It used public endpoints only, under `env -i`, with no keys. The live bot and /workspace/mayo-bot were not touched.
Raw output: `reports/perp_study_2026-10-10T105343Z.json` (per-spec, per-fold, per-symbol and every OOS trade).
Re-run: `python scripts/fetch_futures_charts.py --out /workspace/futures-data && python scripts/study_perp_strategies.py --data /workspace/futures-data --slip-bps 2 5 --proxy`

## Data
- Kraken Futures public charts API (`/api/charts/v1/{trade,mark,spot}/PF_*/1h`), 1h candles for PF_XBTUSD, PF_ETHUSD and PF_SOLUSD from **2022-03-23 (listing) to 2026-10-10 10:00 UTC**, about 4.55 years and 39.9k bars per symbol. This is far longer than the spot ticks (Jun 4 onward). Liquidation is checked on mark high/low. The `spot` chart (Kraken's index) is used as the spot price proxy.
- Real hourly funding (`historical-funding-rates`, no key) is published only for the last year: **2025-10-06 08:00 UTC to 2026-10-10**, about 8,850 hourly prints per perp. The endpoint ignores paging parameters, so nothing older is available.
- Fees: Kraken Futures $0+ tier is **5 bps taker / 2 bps maker**, checked on support.kraken.com/articles/360048917612 on 2026-10-10. The model uses taker on both sides plus 2 bps/side slippage (base case) and 5 bps/side (stress). The spot comparison and the carry spot leg use the repo spot model (40 bps taker + 10 bps slip per side). Kraken's July-2026 schedule lists Tier-1 spot at 0.40% maker / 0.80% taker, so real spot costs could be even higher.
- Liquidation: isolated margin with maintenance 1% (the instrument API lists 0.5% at small size, so 1% is conservative). A liquidated trade loses the full margin (1/lev of notional).

## Method (repo conventions)
- Entries are decided on closed 1h/4h bars, or on the daily close for the D1 rules, and filled at the next bar's open. There are no sub-1h entries and no pure momentum.
- Walk-forward by time: 5 equal segments. The best spec on segment k-1 (by mean net, at least 3 trades, else the default spec) is scored on segment k. Trades are attributed by entry bar and pooled across the 3 perps. The OOS is segments 2-5.
- **Primary window = real funding only**: the year with published funding (OOS 2025-12-19 to 2026-10-10, 295 days).
- **Robustness window = long history**: real perp prices since 2022. Funding before 2025-10-06 is a **proxy**, an OLS of real hourly funding on the hourly mark premium (corr 0.76-0.77 on the overlap, clipped to the 0.5%/h cap). OOS 2023-04-07 to 2026-10-10 (1,282 days). Its prices are real but its funding is not, so it is a sanity check and not the bar.
- Promotion bar (repo `check_strategy`): at least 30 OOS trades, mean > 0 after fees and funding, one-sided 90% lower bound > 0, mean without the best 2 > 0.
- Returns and max DD: equal capital per perp; each trade's notional = leverage x that sleeve's equity; hourly mark-to-market. At 2x the trade list matches 1x (no liquidations happened), so 1x and 2x pass or fail together. Only return and DD scale.
- Configs: 22 rule configs + 2 carry thresholds = 24 signal configs. Run at 1x and 2x (spot only at 1x), that is 59 simulated configs per window, giving 26 family/tf/lev rows scored against the bar per window (13 distinct trade sets). **At a one-sided 90% bound, about 1.3 of 13 no-edge trade sets would "pass" by chance.**

## Families
1. `trend_ls`: repo trend-hold long when D1 is risk-on, bearish mirror short when D1 is not risk-on (EMA20/EMAslow, exit on close across EMAslow). Specs ema_slow 100/80, on 1h and 4h.
   `regime_ls`: repo regime entries long plus their mirror short (ADX hysteresis, EMA stack, N-bar break, D1 gate; chandelier/chop exits). Specs (20,3), (55,3), (20,2), on 1h and 4h.
2. `funding_fade`: f24 = mean known hourly funding over 24h, ranked against the trailing 30 days. Short the top tail, long the bottom tail, fixed hold. q in {0.90, 0.95} x hold in {24h, 72h}.
   `carry`: long spot (index proxy) + short perp at equal notional while the 72h mean funding annualised is above 10% or 20%; exit when it turns negative. Both legs' fees are included. Return is on total capital (spot plus perp margin).
3. `trend_long_perp` / `trend_long_spot` and `d1_long_perp` / `d1_long_spot`: the same long-only signals on perps (5+2 bps + funding) and on spot (40+10 bps).
4. `d1_flip_ls`: long while D1 is risk-on, short while bear, flat otherwise. Bear is either "strict" (close < SMA50 and SMA50 falling vs 5 days ago) or "repo" (known and not risk-on). Acts on the daily close.
   `donchian_ls`: turtle channels on 4h bars (N-day high/low entry, M-day opposite exit), (20,10) and (55,20), each with and without an ATR-rank >= 0.5 volatility filter. Note: the repo refuses "breakout" for *spot* entries, so this is research only.

## Verdict

**One config technically clears the bar on the real-funding OOS: `d1_flip_ls` (daily-filter long/short), 1h execution, 1x or 2x.** But it is **not robust enough to "lock in"** as a proven edge. It is coded as a default-off SHADOW sleeve only.

d1_flip_ls, real-funding window (OOS 2025-12-19 to 2026-10-10, 295 d), walk-forward (picked strict, repo, strict, repo per fold):
- 37 trades (20 long / 17 short). Mean +308.3 bps, 90% LCB +36.0, ex-best-2 +118.1, median -103.9, win 32.4%, mean funding -12.9 bps/trade, 0 liquidations.
- OOS return +37.45% at 1x (max DD 16.83%) and +74.61% at 2x (max DD 26.32%). Buy-and-hold equal-weight over the same window: -10.37% (max DD 50.99%).
- At 5 bps/side slippage it still passes: mean +302.3, LCB +30.0, ex-best-2 +112.1.
- Fixed specs (no selection): strict 36 trades, mean +314.8, LCB +37.1. Repo 38 trades, mean +375.3, LCB +94.6. Both pass.

Why it should not be called proven:
- **Barely enough trades, and concentrated.** 37 trades, median -104 bps, 32% wins. The result rests on about 8 large trend trades: three Jan-Mar 2026 shorts (+28/+18/+26%), the ETH long from Jul 25 (+31%), and the BTC and SOL longs from Aug 18/19 (+28/+41%). Those last two are **still open at the window end** and are marked at the last close. One fold out of four (Mar-May 2026) was negative (-314 bps mean). SOL alone lost through April-May whipsaws.
- **Multiple testing.** 13 distinct trade sets were scored at a one-sided 90% bound, so about 1.3 chance passes are expected with no edge at all. One pass fits luck.
- **Long-history check (real prices, proxy funding, 1,282-day OOS): the walk-forward version FAILS the bar.** 163 trades, mean +215.6, LCB -25.6, ex-best-2 +24.5, median -207.6, win 27%. Return +81.0% at 1x with **max DD 69.8%** (2x: +127%, DD 87.8%), against equal-weight buy-and-hold of +219.9% (DD 69.3%). The fixed specs pass only narrowly (LCB +6.7 strict, +5.4 repo) with 63-72% DD.
- **The short leg is regime-dependent.** In the long window its 79 shorts averaged **-105.5 bps** (sum -8,336 bps). All of the long-history edge came from the long side (+517.7 bps mean). The short leg's real-window profit (+336 bps over 17 trades) is a feature of this year's bear market.

Nothing else clears on the real-funding window:
- **Donchian (4h, both directions)** is the most consistent idea across both windows. Real window: 18 trades, mean +559.9, LCB +99.3, ex-best-2 +251.4, median +383.2, win 50%, +31.8% at 1x (DD 21.1%). It **fails only min_trades (18 < 30)**. Long window: 58 trades, mean +866.2, LCB +188.0, ex-best-2 +332.2, +198.7% at 1x (DD 50.6%; at 2x +326.9%, DD 82.9%); it passes, but on proxy funding and breakout is repo-refused. Worth tracking, not promotable on this evidence.
- **trend_ls / regime_ls 1h** lose after costs in both windows (mean -0.8 to -6.5 bps, LCB well below 0). That reproduces the PR #12 finding: 1h trend entries do not beat fees.
- **trend_ls 4h**: real window mean +72.3 but LCB -15.8 (fail). Long window passes narrowly (LCB +3.7, DD 49.8% at 1x), but its shorts lost there (-10.6 bps mean).
- **funding_fade** loses in both windows (real: 202 trades, mean -32.2 bps; long: -14.1 bps).
- **carry** fails on real funding: 13-14 trades, mean -85 to -97 bps, -1.8% / -3.0%. Real funding to shorts over the OOS was only 0.1-3.6% annualised. It "passes" only on proxy funding, and even there it made +12.4% over 3.5 years at 1x (about 3.4%/yr, below cash), and that rests on the proxy.
- **Perps vs spot (long-only, same signal):** perps beat spot net of funding because fees are lower. Real window 4h trend-long: perp +114.5 vs spot +45.1 bps. 1h: perp +0.5 vs spot -81.2. Long window 4h: perp +129.0 vs spot +82.6. But no long-only variant clears the bar on the real-funding window (4h perp: LCB -23.0, ex-best-2 -7.4; d1_long: 20 trades). On the long window 4h trend-long perp and d1-long perp pass (proxy funding).

## What was written (PR feat/futures-shadow-sleeves; shadow-only)
- `scripts/fetch_futures_charts.py`: public perp candles + funding cache (default /workspace/futures-data).
- `src/dublin_bot/perp_research.py`, `src/dublin_bot/perp_strategies.py`, `scripts/study_perp_strategies.py`: this study. The full JSON outputs (about 180k lines) are not committed; re-run to regenerate them.
- `src/dublin_bot/futures_shadow.py` + `scripts/futures_shadow.py` (`--sleeve d1flip|donchian4h|all --status|--once|--promotion`): two SHADOW sleeves.
  - **d1flip**: 1h checks, strict bear, 1x.
  - **donchian4h**: N=55/M=20, both directions, no vol filter, 1x. That is the spec that passed fixed on the long window and was positive on the real-funding window. N=20/M=10 trades about twice as often, so it would reach 30 forward trades sooner, but it is not the default.
  - Each keeps a virtual ledger only (`logs/futures_shadow_<name>{.jsonl,_trades.jsonl,_state.json}`). There are no paper-book or learner writes and no order route. Data is public charts/ticker/funding only.
  - Real published funding is accrued hourly. The bar is checked at most once per new 1h/4h bar.
  - In the loop, the shadows run in daemon threads with a 3 s join budget, a process-wide rate limit and 8 s HTTP timeouts. Failures are logged as WARN and swallowed, with a 4-minute backoff.
  - Flags (all default false): `FUTURES_SHADOW_D1FLIP_ENABLED`, `FUTURES_SHADOW_DONCHIAN_ENABLED`.
  - Note: on first start d1flip joins the current daily regime at the mark (the research entered on flips). Donchian enters only on a fresh closed-bar breakout, and an opposite breakout flips in the same check.
- Tests: `tests/test_perp_research.py`, `tests/test_futures_shadow.py`.

## Full tables

### Window `real_funding`, slippage 2 bps/side

Only the period with Kraken's published hourly funding history. Data 2025-10-06 07:00 UTC -> 2026-10-10 10:00 UTC; OOS 2025-12-19 02:48 UTC -> 2026-10-10 10:00 UTC (295.3 days); configs simulated 59.

Buy-and-hold over the OOS window (spot index, 50 bps/side): BTC -5.61%, ETH -15.04%, SOL -10.48%, equal-weight -10.37% (max DD 50.99%).

| family | tf (min) | lev | OOS trades | mean bps | 90% LCB bps | mean ex-best-2 | median | win | mean funding bps | liqs | OOS return % | max DD % | bar |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| trend_ls | 60 | 1x | 425 | -0.8 | -23.9 | -14.6 | -55.7 | 0.165 | -0.7 | 0 | -8.44 | 29.95 | fail: mean_positive, lower_bound_positive, ex_best_2_positive |
| trend_ls | 60 | 2x | 425 | -0.8 | -23.9 | -14.6 | -55.7 | 0.165 | -0.7 | 0 | -25.76 | 49.92 | fail: mean_positive, lower_bound_positive, ex_best_2_positive |
| trend_ls | 240 | 1x | 112 | 72.3 | -15.8 | 18.9 | -109.7 | 0.188 | -2.9 | 0 | 22.65 | 28.65 | fail: lower_bound_positive |
| trend_ls | 240 | 2x | 112 | 72.3 | -15.8 | 18.9 | -109.7 | 0.188 | -2.9 | 0 | 36.96 | 48.11 | fail: lower_bound_positive |
| regime_ls | 60 | 1x | 167 | -1.8 | -32.9 | -20.3 | -66.2 | 0.317 | -0.7 | 0 | -2.57 | 27.03 | fail: mean_positive, lower_bound_positive, ex_best_2_positive |
| regime_ls | 60 | 2x | 167 | -1.8 | -32.9 | -20.3 | -66.2 | 0.317 | -0.7 | 0 | -7.91 | 46.86 | fail: mean_positive, lower_bound_positive, ex_best_2_positive |
| regime_ls | 240 | 1x | 42 | 137.9 | -20.3 | 19.3 | -119.0 | 0.333 | -3.5 | 0 | 16.8 | 23.05 | fail: lower_bound_positive |
| regime_ls | 240 | 2x | 42 | 137.9 | -20.3 | 19.3 | -119.0 | 0.333 | -3.5 | 0 | 27.8 | 40.56 | fail: lower_bound_positive |
| d1_flip_ls | 60 | 1x | 37 | 308.3 | 36.0 | 118.1 | -103.9 | 0.324 | -12.9 | 0 | 37.45 | 16.83 | PASS |
| d1_flip_ls | 60 | 2x | 37 | 308.3 | 36.0 | 118.1 | -103.9 | 0.324 | -12.9 | 0 | 74.61 | 26.32 | PASS |
| funding_fade | 60 | 1x | 202 | -32.2 | -59.4 | -42.5 | -8.8 | 0.485 | 1.2 | 0 | -21.57 | 22.29 | fail: mean_positive, lower_bound_positive, ex_best_2_positive |
| funding_fade | 60 | 2x | 202 | -32.2 | -59.4 | -42.5 | -8.8 | 0.485 | 1.2 | 0 | -41.36 | 42.48 | fail: mean_positive, lower_bound_positive, ex_best_2_positive |
| donchian_ls | 240 | 1x | 18 | 559.9 | 99.3 | 251.4 | 383.2 | 0.5 | -24.0 | 0 | 31.81 | 21.05 | fail: min_trades |
| donchian_ls | 240 | 2x | 18 | 559.9 | 99.3 | 251.4 | 383.2 | 0.5 | -24.0 | 0 | 56.28 | 37.23 | fail: min_trades |
| trend_long_perp | 60 | 1x | 202 | 0.5 | -31.4 | -25.6 | -52.7 | 0.153 | -1.2 | 0 | -3.07 | 15.93 | fail: lower_bound_positive, ex_best_2_positive |
| trend_long_perp | 60 | 2x | 202 | 0.5 | -31.4 | -25.6 | -52.7 | 0.153 | -1.2 | 0 | -10.99 | 29.18 | fail: lower_bound_positive, ex_best_2_positive |
| trend_long_spot | 60 | 1x | 199 | -81.2 | -113.8 | -107.9 | -136.5 | 0.101 | 0.0 | 0 | -43.39 | 43.39 | fail: mean_positive, lower_bound_positive, ex_best_2_positive |
| trend_long_perp | 240 | 1x | 48 | 114.5 | -23.0 | -7.4 | -66.6 | 0.25 | -6.2 | 0 | 15.9 | 13.69 | fail: lower_bound_positive, ex_best_2_positive |
| trend_long_perp | 240 | 2x | 48 | 114.5 | -23.0 | -7.4 | -66.6 | 0.25 | -6.2 | 0 | 27.38 | 25.21 | fail: lower_bound_positive, ex_best_2_positive |
| trend_long_spot | 240 | 1x | 45 | 45.1 | -103.2 | -85.9 | -155.9 | 0.244 | 0.0 | 0 | 3.39 | 20.24 | fail: lower_bound_positive, ex_best_2_positive |
| d1_long_perp | 60 | 1x | 20 | 271.0 | -149.0 | -101.3 | -125.1 | 0.25 | -21.0 | 0 | 13.99 | 18.71 | fail: min_trades, lower_bound_positive, ex_best_2_positive |
| d1_long_perp | 60 | 2x | 20 | 271.0 | -149.0 | -101.3 | -125.1 | 0.25 | -21.0 | 0 | 21.24 | 33.68 | fail: min_trades, lower_bound_positive, ex_best_2_positive |
| d1_long_spot | 60 | 1x | 20 | 207.0 | -224.1 | -177.2 | -210.5 | 0.25 | 0.0 | 0 | 9.31 | 21.78 | fail: min_trades, lower_bound_positive, ex_best_2_positive |
| carry_spot_long_perp_short | 60 | 1x | 13 | -84.9 | -98.0 | -97.4 | -93.1 | 0.077 | 25.8 | 0 | -1.82 | 1.82 | fail: min_trades, mean_positive, lower_bound_positive, ex_best_2_positive |
| carry_spot_long_perp_short | 60 | 2x | 14 | -97.0 | -106.7 | -103.3 | -93.9 | 0.0 | 24.0 | 0 | -2.97 | 2.97 | fail: min_trades, mean_positive, lower_bound_positive, ex_best_2_positive |

Funding received by a short over this OOS window: {"BTC/USD": {"hours_with_rate": 7084, "sum_rate_pct": 2.128, "annualised_pct": 2.63}, "ETH/USD": {"hours_with_rate": 7084, "sum_rate_pct": 2.91, "annualised_pct": 3.6}, "SOL/USD": {"hours_with_rate": 7085, "sum_rate_pct": 0.074, "annualised_pct": 0.09}}


### Window `real_funding`, slippage 5 bps/side

Only the period with Kraken's published hourly funding history. Data 2025-10-06 07:00 UTC -> 2026-10-10 10:00 UTC; OOS 2025-12-19 02:48 UTC -> 2026-10-10 10:00 UTC (295.3 days); configs simulated 59.

Buy-and-hold over the OOS window (spot index, 50 bps/side): BTC -5.61%, ETH -15.04%, SOL -10.48%, equal-weight -10.37% (max DD 50.99%).

| family | tf (min) | lev | OOS trades | mean bps | 90% LCB bps | mean ex-best-2 | median | win | mean funding bps | liqs | OOS return % | max DD % | bar |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| trend_ls | 60 | 1x | 425 | -6.8 | -29.9 | -20.6 | -61.7 | 0.16 | -0.7 | 0 | -15.83 | 33.47 | fail: mean_positive, lower_bound_positive, ex_best_2_positive |
| trend_ls | 60 | 2x | 425 | -6.8 | -29.9 | -20.6 | -61.7 | 0.16 | -0.7 | 0 | -37.16 | 54.7 | fail: mean_positive, lower_bound_positive, ex_best_2_positive |
| trend_ls | 240 | 1x | 112 | 66.3 | -21.8 | 12.9 | -115.7 | 0.188 | -2.9 | 0 | 19.98 | 29.38 | fail: lower_bound_positive |
| trend_ls | 240 | 2x | 112 | 66.3 | -21.8 | 12.9 | -115.7 | 0.188 | -2.9 | 0 | 31.13 | 49.13 | fail: lower_bound_positive |
| regime_ls | 60 | 1x | 167 | -7.8 | -38.9 | -26.3 | -72.2 | 0.317 | -0.7 | 0 | -5.76 | 28.63 | fail: mean_positive, lower_bound_positive, ex_best_2_positive |
| regime_ls | 60 | 2x | 167 | -7.8 | -38.9 | -26.3 | -72.2 | 0.317 | -0.7 | 0 | -13.82 | 49.16 | fail: mean_positive, lower_bound_positive, ex_best_2_positive |
| regime_ls | 240 | 1x | 42 | 131.9 | -26.3 | 13.3 | -125.0 | 0.333 | -3.5 | 0 | 15.84 | 23.37 | fail: lower_bound_positive |
| regime_ls | 240 | 2x | 42 | 131.9 | -26.3 | 13.3 | -125.0 | 0.333 | -3.5 | 0 | 25.72 | 41.07 | fail: lower_bound_positive |
| d1_flip_ls | 60 | 1x | 37 | 302.3 | 30.0 | 112.1 | -109.9 | 0.324 | -12.9 | 0 | 36.55 | 17.02 | PASS |
| d1_flip_ls | 60 | 2x | 37 | 302.3 | 30.0 | 112.1 | -109.9 | 0.324 | -12.9 | 0 | 72.58 | 26.67 | PASS |
| funding_fade | 60 | 1x | 202 | -38.2 | -65.4 | -48.5 | -14.8 | 0.47 | 1.2 | 0 | -24.67 | 25.2 | fail: mean_positive, lower_bound_positive, ex_best_2_positive |
| funding_fade | 60 | 2x | 202 | -38.2 | -65.4 | -48.5 | -14.8 | 0.47 | 1.2 | 0 | -45.88 | 46.72 | fail: mean_positive, lower_bound_positive, ex_best_2_positive |
| donchian_ls | 240 | 1x | 18 | 553.9 | 93.3 | 245.4 | 377.2 | 0.5 | -24.0 | 0 | 31.35 | 21.14 | fail: min_trades |
| donchian_ls | 240 | 2x | 18 | 553.9 | 93.3 | 245.4 | 377.2 | 0.5 | -24.0 | 0 | 55.22 | 37.4 | fail: min_trades |
| trend_long_perp | 60 | 1x | 202 | -5.5 | -37.4 | -31.6 | -58.7 | 0.149 | -1.2 | 0 | -6.91 | 17.52 | fail: mean_positive, lower_bound_positive, ex_best_2_positive |
| trend_long_perp | 60 | 2x | 202 | -5.5 | -37.4 | -31.6 | -58.7 | 0.149 | -1.2 | 0 | -17.93 | 31.88 | fail: mean_positive, lower_bound_positive, ex_best_2_positive |
| trend_long_spot | 60 | 1x | 199 | -81.2 | -113.8 | -107.9 | -136.5 | 0.101 | 0.0 | 0 | -43.39 | 43.39 | fail: mean_positive, lower_bound_positive, ex_best_2_positive |
| trend_long_perp | 240 | 1x | 48 | 108.5 | -29.0 | -13.4 | -72.6 | 0.229 | -6.2 | 0 | 14.81 | 14.21 | fail: lower_bound_positive, ex_best_2_positive |
| trend_long_perp | 240 | 2x | 48 | 108.5 | -29.0 | -13.4 | -72.6 | 0.229 | -6.2 | 0 | 25.04 | 26.13 | fail: lower_bound_positive, ex_best_2_positive |
| trend_long_spot | 240 | 1x | 45 | 45.1 | -103.2 | -85.9 | -155.9 | 0.244 | 0.0 | 0 | 3.39 | 20.24 | fail: lower_bound_positive, ex_best_2_positive |
| d1_long_perp | 60 | 1x | 20 | 265.0 | -155.0 | -107.3 | -131.1 | 0.25 | -21.0 | 0 | 13.56 | 18.95 | fail: min_trades, lower_bound_positive, ex_best_2_positive |
| d1_long_perp | 60 | 2x | 20 | 265.0 | -155.0 | -107.3 | -131.1 | 0.25 | -21.0 | 0 | 20.37 | 34.06 | fail: min_trades, lower_bound_positive, ex_best_2_positive |
| d1_long_spot | 60 | 1x | 20 | 207.0 | -224.1 | -177.2 | -210.5 | 0.25 | 0.0 | 0 | 9.31 | 21.78 | fail: min_trades, lower_bound_positive, ex_best_2_positive |
| carry_spot_long_perp_short | 60 | 1x | 13 | -90.9 | -104.0 | -103.4 | -99.1 | 0.077 | 25.8 | 0 | -1.95 | 1.95 | fail: min_trades, mean_positive, lower_bound_positive, ex_best_2_positive |
| carry_spot_long_perp_short | 60 | 2x | 14 | -102.8 | -112.3 | -109.0 | -99.9 | 0.0 | 24.0 | 0 | -3.15 | 3.15 | fail: min_trades, mean_positive, lower_bound_positive, ex_best_2_positive |

Funding received by a short over this OOS window: {"BTC/USD": {"hours_with_rate": 7084, "sum_rate_pct": 2.128, "annualised_pct": 2.63}, "ETH/USD": {"hours_with_rate": 7084, "sum_rate_pct": 2.91, "annualised_pct": 3.6}, "SOL/USD": {"hours_with_rate": 7085, "sum_rate_pct": 0.074, "annualised_pct": 0.09}}


### Window `long_proxy_funding`, slippage 2 bps/side

Perp candles since listing; funding before the published window is a PROXY fitted on the overlap (see funding_proxy). Data 2022-05-22 10:00 UTC -> 2026-10-10 10:00 UTC; OOS 2023-04-07 19:36 UTC -> 2026-10-10 10:00 UTC (1281.6 days); configs simulated 59.

Buy-and-hold over the OOS window (spot index, 50 bps/side): BTC 193.87%, ETH 33.2%, SOL 432.61%, equal-weight 219.89% (max DD 69.34%).

| family | tf (min) | lev | OOS trades | mean bps | 90% LCB bps | mean ex-best-2 | median | win | mean funding bps | liqs | OOS return % | max DD % | bar |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| trend_ls | 60 | 1x | 1901 | -6.5 | -17.4 | -10.5 | -62.9 | 0.186 | -2.5 | 0 | -54.41 | 71.06 | fail: mean_positive, lower_bound_positive, ex_best_2_positive |
| trend_ls | 60 | 2x | 1901 | -6.5 | -17.4 | -10.5 | -62.9 | 0.186 | -2.5 | 0 | -87.61 | 94.55 | fail: mean_positive, lower_bound_positive, ex_best_2_positive |
| trend_ls | 240 | 1x | 502 | 56.5 | 3.7 | 21.4 | -108.8 | 0.227 | -12.0 | 0 | 60.27 | 49.75 | PASS |
| trend_ls | 240 | 2x | 502 | 56.5 | 3.7 | 21.4 | -108.8 | 0.227 | -12.0 | 0 | 57.05 | 78.39 | PASS |
| regime_ls | 60 | 1x | 668 | -5.9 | -23.8 | -12.4 | -82.7 | 0.341 | -2.2 | 0 | -19.9 | 41.02 | fail: mean_positive, lower_bound_positive, ex_best_2_positive |
| regime_ls | 60 | 2x | 668 | -5.9 | -23.8 | -12.4 | -82.7 | 0.341 | -2.2 | 0 | -45.65 | 67.73 | fail: mean_positive, lower_bound_positive, ex_best_2_positive |
| regime_ls | 240 | 1x | 241 | -7.5 | -70.6 | -49.8 | -177.8 | 0.315 | -10.2 | 0 | -18.57 | 51.3 | fail: mean_positive, lower_bound_positive, ex_best_2_positive |
| regime_ls | 240 | 2x | 241 | -7.5 | -70.6 | -49.8 | -177.8 | 0.315 | -10.2 | 0 | -44.5 | 79.13 | fail: mean_positive, lower_bound_positive, ex_best_2_positive |
| d1_flip_ls | 60 | 1x | 163 | 215.6 | -25.6 | 24.5 | -207.6 | 0.27 | -44.9 | 0 | 81.0 | 69.77 | fail: lower_bound_positive |
| d1_flip_ls | 60 | 2x | 163 | 215.6 | -25.6 | 24.5 | -207.6 | 0.27 | -44.9 | 0 | 127.12 | 87.8 | fail: lower_bound_positive |
| funding_fade | 60 | 1x | 965 | -14.1 | -31.9 | -17.5 | 3.6 | 0.508 | 3.9 | 0 | -41.08 | 63.89 | fail: mean_positive, lower_bound_positive, ex_best_2_positive |
| funding_fade | 60 | 2x | 965 | -14.1 | -31.9 | -17.5 | 3.6 | 0.508 | 3.9 | 0 | -69.06 | 87.92 | fail: mean_positive, lower_bound_positive, ex_best_2_positive |
| donchian_ls | 240 | 1x | 58 | 866.2 | 188.0 | 332.2 | 171.3 | 0.552 | -112.0 | 0 | 198.66 | 50.61 | PASS |
| donchian_ls | 240 | 2x | 58 | 866.2 | 188.0 | 332.2 | 171.3 | 0.552 | -112.0 | 0 | 326.93 | 82.88 | PASS |
| trend_long_perp | 60 | 1x | 872 | 9.9 | -8.9 | 1.4 | -66.1 | 0.181 | -6.3 | 0 | 8.62 | 42.78 | fail: lower_bound_positive |
| trend_long_perp | 60 | 2x | 872 | 9.9 | -8.9 | 1.4 | -66.1 | 0.181 | -6.3 | 0 | -18.7 | 71.86 | fail: lower_bound_positive |
| trend_long_spot | 60 | 1x | 895 | -66.4 | -85.4 | -74.8 | -146.0 | 0.131 | 0.0 | 0 | -88.7 | 89.29 | fail: mean_positive, lower_bound_positive, ex_best_2_positive |
| trend_long_perp | 240 | 1x | 237 | 129.0 | 29.9 | 55.1 | -115.7 | 0.249 | -29.0 | 0 | 88.49 | 32.96 | PASS |
| trend_long_perp | 240 | 2x | 237 | 129.0 | 29.9 | 55.1 | -115.7 | 0.249 | -29.0 | 0 | 115.86 | 59.68 | PASS |
| trend_long_spot | 240 | 1x | 233 | 82.6 | -21.6 | 5.5 | -191.9 | 0.223 | 0.0 | 0 | 27.48 | 49.79 | fail: lower_bound_positive |
| d1_long_perp | 60 | 1x | 84 | 513.8 | 71.0 | 144.4 | -153.1 | 0.31 | -99.4 | 0 | 108.89 | 51.11 | PASS |
| d1_long_perp | 60 | 2x | 84 | 513.8 | 71.0 | 144.4 | -153.1 | 0.31 | -99.4 | 0 | 164.31 | 74.64 | PASS |
| d1_long_spot | 60 | 1x | 84 | 529.6 | 66.3 | 148.7 | -236.1 | 0.274 | 0.0 | 0 | 104.87 | 52.78 | PASS |
| carry_spot_long_perp_short | 60 | 1x | 35 | 205.1 | 119.4 | 137.5 | 58.7 | 0.571 | 284.1 | 0 | 12.42 | 1.16 | PASS |
| carry_spot_long_perp_short | 60 | 2x | 40 | 195.7 | 129.0 | 147.2 | 107.1 | 0.625 | 241.6 | 0 | 18.58 | 1.06 | PASS |

Funding received by a short over this OOS window: {"BTC/USD": {"hours_with_rate": 30758, "sum_rate_pct": 37.848, "annualised_pct": 10.78}, "ETH/USD": {"hours_with_rate": 30758, "sum_rate_pct": 36.767, "annualised_pct": 10.47}, "SOL/USD": {"hours_with_rate": 30758, "sum_rate_pct": 29.33, "annualised_pct": 8.35}}

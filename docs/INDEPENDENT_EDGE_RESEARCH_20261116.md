# MAYO independent edge candidates — preregistered research plan
**Frozen research proposal, 16 Nov 2026. Paper-only. No exchange writes, live trading, auto-promotion, or active-runner changes.**

## Scope and evidence boundaries
The operator/Nova report: MAYO targets VET, HBAR, ALGO and XLM on Binance spot; verify whether the actual venue is **Binance.US** versus **Binance.com**, and verify exact quote currencies and fee tiers. Neither the 58-trade "Capit 24 L" candidate (PF 2.47 after fees, avg ~3.1%) nor its 530 searched variations is independent evidence of forward profitability. At most 5 reported recent paper signals, none filled; get their actual time windows and public prints before assigning a cause. Keep blocked PR #18 isolated.

These are **two independent hypotheses**, not licenses to modify the primary trading method. Use one fixed protocol per candidate, label its first-run test as exploratory if any of the chosen period or coins influenced selection, and collect subsequent genuinely unseen forward results.

## Candidate A — Low-activity short-term reversal (highest research priority)
**Research motivation:** Bianchi, Babiak & Dickerson (2022), *Journal of Banking & Finance* 142:106547, found liquidity-provision/reversal returns concentrated in lower-activity crypto pairs. Their reported portfolio results cannot be assumed for VET/HBAR/ALGO/XLM or as executable limit fills. Zaremba et al. (2021) found cross-sectional next-day reversal effects depend on asset liquidity, unlike the biggest coins.

**Frozen first statistical test (no orders):**
- Use the same four currently reported spot coins and one verified Binance venue/quote. All timestamps UTC; no revised/future data; exclude stale or missing bars.
- On each *completed daily candle* t, flag if the 1-day return is negative and in the bottom 25% of that coin's prior 60 completed daily-return observations, and daily notional volume is strictly below that coin's median from the prior 30 completed days. Determine quantiles from only days ending before t.
- Primary test metric: target coin's next 24h close-to-close return after signal t, relative to its unconditional next-day return and a negative-return-only control. Evaluate per coin and pooled, with overlapping signal observations and market trend controlled in inference; report the number of signals.
- Do not choose a different RSI/volume/window/stop after looking at results. This fixed parameterization is an *implementation assumption to test*, not one supplied by the academic paper. A positive conditional next-day return does not prove executable profit.
- If promising, prepare a **second frozen paper-order protocol** that explicitly specifies entry/exit timing, trading fees and slippage. Compare realistic taker bid/ask fills against passive limit queue simulations. No more parameter search on validation dates.

## Candidate B — Cross-coin lagged-return effect (second priority)
**Research motivation:** Guo, Sang, Tu & Wang (2024), *Journal of Economic Dynamics and Control* 163:104863, document positive cross-cryptocurrency return predictability on Binance; Jia et al. (2023), *Journal of Empirical Finance* 74:101428, report negative intraday seesaw effects, so **direction is not universal**. A recent October 2026 paper on lead-lag data snooping found its attractive result failed holdout and multiple-test checks.

**Frozen first statistical test (no orders):**
- For each target coin i at the end of closed hour t, predictor = equal-weight mean of the *other three coins'* return from completed hour t-1 to t. Target = coin i return from t to t+1. This is one predeclared predictor family across all four coins: do not cherry-pick favorable leader/follower pairs.
- Train coefficient signs/weights on a chronological training period, freeze them, and evaluate on a chronologically later untouched holdout with a gap if any lookback labels overlap. Report direction accuracy **and** bps forecast calibration, with always-flat, simple trend and market-exposure controls; use robust inference across time.
- A negative or unstable predicted effect must be reported as a failed candidate. A statistical effect smaller than spread+fees is not a tradable edge. Do not treat simultaneously moving coins as lead-lag.
- Only after a genuine predictive edge survives holdout may the coder write a separate deterministic paper-entry rule and paper scoring. No BTC execution; if BTC used as an additional external control, read-only and predeclared.

## Required guardrails and reproducible evidence
1. Label source venue (binance_us/binance_com), quote (USD/USDT), pair metadata, timezone, listed history, fee schedule effective date, tick/lot/min-notional precision, missing bars and public trades. Binance.com historical prices are not automatically executable Binance.US prices.
2. Keep signal timestamps, features, event identifiers, cost model and training/holdout boundaries immutable after registration. Keep selected historical periods out of genuine future test claims if they were used during discovery or coin selection.
3. Report base strategy, each candidate separately, and combined **only after each separately passes**. Never sneak B into A after seeing returns. Specify exits before paper tests. Measure performance with maker queue uncertainty, unfilled orders, trade-through versus touch, fees, spread, adverse selection and costs under stress.
4. Per coin and aggregate: number of independent signals, simulated limit attempts, touch/through/miss counts, complete paper trades, profit factor after actual modeled costs, net expectancy, median, max drawdown, worst week, performance excluding largest two winners, exposure and capital use. Compare to the current four-coin paper baseline and buy/hold at identical dates/size.
5. No threshold is a guarantee; >=30 new, truly out-of-sample *closed paper trades* and PF >=1.10 after realistic costs are **minimum research gates**, plus robustness across coins/folds and acceptable drawdown. Do not launch a live bot or auto-promote a strategy based on a backtest.

## Source links
- https://doi.org/10.1016/j.jbankfin.2022.106547
- https://doi.org/10.1016/j.irfa.2021.101908
- https://doi.org/10.1016/j.jedc.2024.104863
- https://doi.org/10.1016/j.jempfin.2023.101428
- https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7659841

## Handoff / status
This document only registers research. It has **not** collected market data, run these candidate studies, verified the existing external Binance runner, or found a profitable forward edge. The separate read-only limit-fill checker on this branch is an evidence tool, not a trading strategy or a claim of filled orders. Keep the active runner unmodified until the coder reconciles the actual deployed code and can review a separate implementation PR.

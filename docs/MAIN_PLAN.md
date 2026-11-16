# MAIN PLAN (2026-11-16): Binance.US strategies, paper only

**Owner decisions from the 2026-11-16 voice calls. PAPER ONLY. Live is locked.**
`PAPER_TRADING=true`, `DRY_RUN=true`, `ALLOW_LIVE_TRADING=false`, futures/margin live flags off.
Nothing here changes a flag, holds a key, reads `.env`, or calls an order endpoint.

## 0. Venue rule
- **Binance.US is the only venue for strategy trading and paper running.** Live bars, paper fills
  and the primary fee books are all Binance.US.
- **Kraken is the vault**: holding, and deliberate manual selling only. No strategy executes on
  Kraken. Kraken cost columns (80 / 160 bps) appear in paper reports as **reference only**.
- **No Coinbase and no Kraken execution.** Coinbase was used once, as an independent *research*
  data source to verify TREND-6 (`research/edge6/`). No runner uses it.
- **History gap:** Binance.US's own price history has a **19-month hole (2023-07 → 2025-02, USD
  pairs suspended)**. Backtests therefore used **Binance global prices (data.binance.vision
  BTCUSDT/ETHUSDT and `<COIN>USDT`) with Binance.US fees**. The TREND-6 runner does the same only
  to warm up its 200-day average when Binance.US lacks 200 contiguous days, and it records which
  source fed each decision. Today Binance.US has 635 contiguous daily bars, so no warm-up is used.

## 1. Strategies and runners

| Strategy | What | Runner | State (2026-11-16 11:11 CT) |
|---|---|---|---|
| **CAPIT-24** | Hourly crash-buy on VET, HBAR, ALGO, XLM; market entry, 48 h exit, 2 × $50 on $100 | `paper/capit_binanceus/` (`scripts/capit_paper.py`), pid 1562619 | **Running**, untouched. This is what checks VET/HBAR/ALGO/XLM every hour |
| **CAPIT-24L** | CAPIT-24 signal with a post-only limit entry at 0.995 × close, 60 min | `paper/capit24l_binanceus/` | **Built, NOT started**, not in autostart. Rules: [MAIN_PLAN_CAPIT24L_BINANCEUS.md](MAIN_PLAN_CAPIT24L_BINANCEUS.md) |
| **TREND-6** | Daily trend ensemble on BTC + ETH, long or cash | `paper/trend6_binanceus/` (`scripts/trend6_paper.py`, `src/dublin_bot/trend6.py`) | **Started 2026-11-16 11:11 CT (owner-approved), pid 1618392, paper only, $100, 100% cash**. In `scripts/capit_autostart.sh` |
| Shadow | CAPIT-24 signal-only shadow on a 12-coin list | `shadow/capit24/` pid 1562655 | Running, untouched |

TREND-6 is **separate from CAPIT**: its own $100 paper book, its own ledger, no shared cash.

## 2. TREND-6 rules (frozen; do not retune mid-test)

| Item | Rule |
|---|---|
| Coins | BTC, ETH (Binance.US `BTCUSD`, `ETHUSD`) |
| Decision | After each **00:00 UTC daily close** D, per coin, 6 yes/no signals on closes ≤ D: close > SMA50, SMA100, SMA200; 7-, 14-, 28-day return > 0 |
| Size | Target weight per coin = **50% × (yes / 6)** of book equity (steps of 1/12). Max 100% invested, rest cash |
| Execution | At the **open of day D+1** (Binance.US 1d open; fallback first 1h open). Catch-up after downtime decides every missed day in order |
| Band | A coin trades **only when its target changes**. Drift is never traded. Changes under the Binance.US **$1 minimum** are skipped and logged (`skipped.jsonl`) |
| Fee books | **`binanceus` PRIMARY**: 0.02% taker + 2 bps slip per side (8 bps RT); 0% maker is the best case and isn't assumed. **`binanceus10`**: conservative, 10 bps RT (the verified backtest model). `ref_kraken80`, `ref_kraken160`: **reference columns only** |
| Kills (primary book, latched) | **K-DD**: max drawdown **> 45%**, any time (unchanged). **K-REL** (owner-approved 2026-11-16, with the below-start gate added the same day; replaces "PF < 1.10 after 30"), only once **≥ 30 closed round trips** exist, fires when ALL hold: (a) return since paper start < the 50/50 buy-and-hold return over the same period, (b) equity **> 10% below its running peak**, (c) equity **below its level 30 days earlier**, (d) equity **below the $100 starting equity**. Evaluated at each daily open. Killed: no new buys, exits still run, status shows `killed` + `kill_inputs` (includes `below_start`). The owner reviews; no auto-restart of a killed config |
| Trade | A round trip = a coin goes from 0 to held and back to 0 in one book |
| Benchmark | 50/50 BTC/ETH bought at the first execution open, held, no cost (`bench_5050_hold`) |

**Kill-rule history check (`research/edge6/kill_rule_history.py`, the actual runner replayed on
Binance global daily data, 26 quarterly paper starts 2019-01 → 2025-04, first 2 years each):**

| Rule | Fired within 2 years |
|---|---|
| Old: PF < 1.10 over the *first 30* round trips | 7 / 26 |
| Old: PF < 1.10 checked after every close from trip 30 on (as the runner had it) | 9 / 26 |
| K-REL without the below-start gate (first 2026-11-16 approval; replaced the same day) | 15 / 26 |
| **K-REL + equity below the $100 start (now live in the runner; owner-approved 2026-11-16 11:26 CT)** | **3 / 26** |
| K-DD 45% | 0 / 26 |

K-REL without the below-start gate fired more often than the PF rule it replaced. Most of those
kills came in bull markets: 2019-07 start, strategy +381% vs B&H +436% with a 12% pullback; 2021-01
start, +135% vs +157% in the May-2022 slide. Trailing buy-and-hold in strong up years is normal for
TREND-6 (it beat B&H in only 4 of 8 years), so "behind B&H + >10% off peak + falling" happens in
almost every bull-run correction once 30 trips exist. The owner approved adding **strategy equity
below its $100 start**, which cuts the historical trip rate to 3/26 and is now the live rule. In
practice K-REL still can't fire for about 13 months (0 trips today; equity is at the $100 start).

## 3. TREND-6 verification verdict (commit `ff9bbc9`, `reports/trend6_verification_2026-11-16.md`)
- **Most credible strategy found in this project. Worth a Binance.US paper run. Not ready for live.**
- Independent rebuild on Coinbase data: CAGR 56.7% vs B&H 49.5%, Sharpe 1.27 vs 0.94, max DD −42% vs
  −76%, 2022 −24% vs −65%, up 7 of 8 years. Exposure-matched B&H (55%) makes only 32%.
- Corrections: **PF ≈ 5.1** (not 6-7; fee-counting bug); weight changes on ≈130 days per coin per
  year in ≈$8 steps on $100 (13/yr is the in/out round-trip count); turnover ≈28× a year.
- **Luck p ≈ 0.01-0.02** on fair nulls (circular shift 0.003, iid bootstrap 0.008, 5-day block
  0.024); **0.17 on the strictest** (20-day block). TREND-6 is about the **548th variant** tried in
  this project.
- **No look-ahead.** A one-day execution delay still gives 53% a year.
- Venue: at Kraken taker (160 bps) it's flat since 2024. Binance.US costs only.

## 4. Required before any live go-ahead (all strategies)
1. **Binance.US cost and slippage sensitivity on real fills**: paper fills vs the Binance.US
   open, and drift > 20 bps from the model is a stop. The primary book assumes 8 bps RT.
2. **Forward paper testing**: TREND-6 needs ≥ 30 closed round trips, about **13-14 months** at
   ~27 a year, with PF ≥ 1.10 and no 45% drawdown. CAPIT-24/24L: ≥ 30 closed trades
   (realistically 9-12 months).
3. An **always-on host** (the box sleeps and catches up).
4. **Per-flag OK from the owner** for every live flag, one at a time.
5. **The owner's own Binance.US API keys**, set up by him, trade-only, no withdrawal permission.
   Agents never hold or read keys.

## 5. Market snapshot 2026-11-16 ~11:00 CT (Binance.US)
BTC **$64,873** (−26% from its 90-day high of ~$87,388; −12.6% over 7 days), ETH **$1,828.67**
(−35% from its 90-day high). Both are below their 50/100/200-day averages with negative
7/14/28-day returns: **all six TREND-6 signals are off for both coins, so TREND-6 is 100% cash.**
The runner's first decision (day 2026-11-15, executed at the 2026-11-16 00:00 UTC open) matched
that: 0/6 for BTC and ETH, $100 cash.
VET was −9% on the day, the kind of move CAPIT watches. The **running CAPIT-24 runner** (pid
1562619) does the hourly VET/HBAR/ALGO/XLM checks. CAPIT-24L isn't running.

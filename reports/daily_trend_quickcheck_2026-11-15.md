# Daily trend-following quick check — BTC, ETH, SOL

November 15, 2026 · research only · paper-trading implication: **$100 Kraken spot, no live orders**

**Verdict: NO-GO for a $100 Kraken spot test of daily time-series trend following.**

The primary rule is a close copy of Zarattini, Pagani and Barbon (2025), applied to BTC, ETH and SOL only, with sleeve leverage capped at 100% so it can run on spot. At Kraken tier-1 maker fees (80 bps round trip) on a fresh $100 book from 1 Jan 2024 to 14 Nov 2026, it does not clear the promotion bar. Reasons: yearly folds in the test window were 1/3 positive (need a majority); 92% of intended $100-account fills are below Kraken minimums (median notional $1.88); test return 8.5% does not beat buy-and-hold 12.7% (zero-cost) / 12.2% (80 bps entry).

## 1. Direct answer

**Primary rule (pre-registered, not picked after the fact):** equal-weight ensemble of nine long-only Donchian models on *closes* with lookbacks 5, 10, 20, 30, 60, 90, 150, 250 and 360 days. Enter when the close makes an n-day closing high. Exit when the close falls through a trailing stop that starts at the Donchian midpoint and only ratchets up. Each sleeve is sized to 25% annualized volatility using 90-day realized volatility, capped at 100% of that coin’s one-third sleeve (so the book never needs margin). Volatility-driven rebalances fire only if the sleeve weight drifts by more than 20 percentage points; entries and exits always fill. Capital is split equally across whichever of BTC, ETH and SOL have finished warmup. Signals are taken at the daily close and filled at the next day’s open.

That is the Zarattini et al. 2025 Combo rule, with two intentional changes for a $100 Kraken *spot* account: the paper’s 200% sleeve cap is cut to 100%, and the paper’s 20-coin monthly rotational universe is replaced by these three names. The 200% version is reported as a reference variant; it is not tradable on cash spot.

**$100 test, 80 bps round trip, 1 Jan 2024 – 14 Nov 2026 (primary):** 100 trades, win rate 15%, avg win $1.46 / avg loss $-0.16, payoff 9.29, profit factor 1.64, CAGR 2.4%, max drawdown -14.5%, Sharpe 0.32, total return 8.5% ($100 → $108.55). Yearly folds: 1/3 positive (2024 +18.3%; 2025 -4.6%; 2026 -3.8%).

**Buy-and-hold, same window:** zero-cost equal-weight 12.7% (CAGR 2.4%, max DD -64.3%, Sharpe 0.37); with an 80 bps entry cost 12.2%. Cash is 0%. Primary beats cash and does not beat buy-and-hold.

**2026 YTD (1 Jan – 14 Nov 2026), 80 bps, primary on the continuing full-sample book:** return -3.8%, Sharpe -0.59, max DD -6.9%, trades 48, PF 0.40. Buy-and-hold zero-cost YTD -30.9%.

**Bar (test $100, 80 bps, primary):** trades ≥30: yes; PF ≥1.10: yes; majority of yearly folds positive: no (1/3); beats cash: yes. **Overall bar: FAIL.**

**Empirically best of the 12 on this same $100 80 bps test (selection-biased, not the pre-registered call):** `sma200_vol` — 40 trades, PF 2.31, CAGR 6.2%, Sharpe 0.47, max DD -13.4%, total return 21.5%. This is reported for completeness; it is not a reason to go live.

**Go/no-go:** **NO-GO.** yearly folds in the test window were 1/3 positive (need a majority) 92% of intended $100-account fills are below Kraken minimums (median notional $1.88) test return 8.5% does not beat buy-and-hold 12.7% (zero-cost) / 12.2% (80 bps entry)

## 2. Data, costs, and minimum size

- **Prices:** Binance Vision public spot daily klines (USDT pairs). `api.binance.com` returned HTTP 451 from this host, so the S3 Vision archive was used. BTC and ETH run 17 Aug 2017 – 14 Nov 2026; SOL runs 11 Aug 2020 – 14 Nov 2026. The 15 Nov 2026 bar was still open and was dropped. Signals use UTC daily closes; fills use the next UTC open.
- **Kraken check:** public OHLC `interval=1440` only returns 720–721 bars (~25 Nov 2024 – 15 Nov 2026). On the overlap, Kraken USD vs Binance USDT closes were tight: BTC median basis -1.9 bps, mean absolute 5.6 bps, ETH median basis -2.0 bps, mean absolute 5.7 bps, SOL median basis -1.7 bps, mean absolute 5.9 bps. The primary rule was re-run on Kraken prices in that overlap as a robustness check (section 6).
- **Costs:** 10 bps round trip (paper’s published net figure, and a Binance-like reference), 80 bps (Kraken tier-1 maker 40 bps each way, no slippage — best case), 160 bps (stress; about taker 80 bps each way). There is no mid-tier. All reported P&L below is net of the stated cost.
- **Kraken minimums** (public `AssetPairs`, 15 Nov 2026): BTC `ordermin` 0.00006, ETH 0.002, SOL 0.05; `costmin` $0.50 on all three. At the 14 Nov 2026 Binance close (BTC $64150, ETH $1769.20, SOL $70.28) the binding minimum notionals are about **$3.85 / $3.54 / $3.51**. A $100 book split three ways is ~$33 per coin, which clears those minimums for a full sleeve entry. Volatility downsizing and small rebalances may not.
- **$100 test fills, primary, 80 bps:** 938 intended fills, 867 (92%) below the Kraken minimum, accounting for 80% of turnover. Median fill notional $1.88 (range $0.03–$22.02). The backtest *does not* skip those undersize fills — it assumes they go through — so live P&L would be slightly worse if they are rejected. Entries and exits are usually large enough; the undersize tickets are mostly volatility rebalances.
- **Warmup:** Combo needs 360 daily bars (and 90 days of returns for vol). BTC/ETH can trade from August 2018; SOL joins around August 2021. Until SOL is ready, capital is split equally across the coins that are ready. Buy-and-hold does the same, then adds SOL with a one-time rebalance.

## 3. Paper rule, and what we changed

Source: Carlo Zarattini, Alberto Pagani, Andrea Barbon, “Catching Crypto Trends; A Tactical Approach for Bitcoin and Altcoins,” Swiss Finance Institute Research Paper No. 25-80, this version 9 April 2025, [SSRN 5209907](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5209907), PDF from [Concretum](https://concretumgroup.com/wp-content/uploads/2026/02/Catching-Crypto-Trends.pdf).

Copied exactly:
- Donchian channels computed on *closes*, not highs/lows: upper = max of the last n closes, lower = min, mid = average of the two.
- Enter when close equals the upper band; stay long otherwise; exit when close ≤ trailing stop.
- Trailing stop used on day t+1 is max(stop used on day t, mid on day t). It never ratchets down. New trades start with stop = mid at entry.
- Nine lookbacks: 5, 10, 20, 30, 60, 90, 150, 250, 360. Combo weight is the equal-weight average of the nine sleeve weights.
- Sleeve weight = min(0.25 / σ_t, cap) × position, with σ_t = 90-day (3-month) annualized standard deviation of daily returns, annualized with 365 days.
- 20% rebalance threshold applies only to volatility-driven weight changes; a new entry or stop fills immediately.

Changed for this test:
- Universe is BTC, ETH, SOL, not a monthly top-20 volume rotation. Their headline Sharpe 1.57 and 11% max DD is for that 20-coin book, net of 10 bps, 2015–19 Mar 2025. It is not a claim about a 3-coin $100 Kraken account.
- Sleeve cap 100% in the primary (spot). The paper allows 200%. That variant is included; it rarely differs because crypto volatility usually keeps 0.25/σ well under 1.
- Execution is next-open, not close-to-close. Fees are charged on every fill’s absolute weight change.
- Sample runs through 14 Nov 2026, so it includes the post-paper year the authors could not have seen.

**BTC-only close-to-close replication at 10 bps, 200% cap, 20% threshold** (the closest apples-to-apples check we can run on this data): from 2018-08-12 to 2026-11-14, CAGR 16.0%, vol 14.5%, Sharpe 1.10, max DD -19.3%, total return 242.0%. The paper’s BTC Combo, Jan 2015–19 Mar 2025, was 30% CAGR, 17% vol, Sharpe 1.58 *gross* / 1.56 net of 10 bps, max DD 19%. We start later (miss 2015–17) and include 2025–26, so a lower number is not a coding failure by itself; it is a different window. If this Sharpe had come out near zero or negative, that would have been a warning that the rule was mis-implemented.

## 4. All 12 variants at 80 bps

Twelve variants were specified in advance. No other lookbacks or filters were tried. The primary is row 1. Trade counts, PF and win rate are at the *coin-level round trip* (a trade starts when that coin’s weight leaves 0 and ends when it returns to 0; Combo can stay lightly long in a coin while some sleeves are off). CAGR, Sharpe, max DD and total return are on the $100 daily equity path.

### 4.1 $100 book, 1 Jan 2024 – 14 Nov 2026 (the decision window)

| Variant | Trades | Win | PF | CAGR | Max DD | Sharpe | Total return | Final $ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **Zarattini Combo 25% vol, 100% cap (primary)** | 100 | 15% | 1.64 | 2.4% | -14.5% | 0.32 | 8.5% | $108.55 |
| Zarattini Combo 25% vol, 200% cap (paper, not spot-feasible) | 100 | 15% | 1.64 | 2.4% | -14.5% | 0.32 | 8.5% | $108.55 |
| Zarattini Combo equal-weight (no vol target) | 100 | 13% | 1.36 | 2.9% | -30.5% | 0.29 | 12.5% | $112.47 |
| Close > SMA 50, 25% vol, 100% cap | 88 | 23% | 1.65 | 8.4% | -17.3% | 0.65 | 28.8% | $128.77 |
| Close > SMA 100, 25% vol, 100% cap | 77 | 18% | 1.50 | 5.1% | -21.8% | 0.43 | 17.9% | $117.94 |
| Close > SMA 200, 25% vol, 100% cap | 40 | 32% | 2.31 | 6.2% | -13.4% | 0.47 | 21.5% | $121.51 |
| Close > SMA 100, equal 1/N (no vol target) | 77 | 18% | 1.20 | 4.5% | -46.1% | 0.35 | 19.0% | $119.02 |
| Donchian 20-high entry / 10-low exit, 25% vol | 53 | 36% | 0.96 | -1.4% | -22.1% | 0.02 | -1.8% | $98.18 |
| Donchian 20-high entry / 20-low exit, 25% vol | 38 | 42% | 1.09 | 0.7% | -22.3% | 0.17 | 4.4% | $104.36 |
| Donchian 50-high entry / 10-low exit, 25% vol | 34 | 50% | 1.23 | 1.2% | -13.2% | 0.24 | 6.1% | $106.08 |
| Donchian 50-high entry / 20-low exit, 25% vol | 26 | 50% | 1.21 | 1.2% | -14.7% | 0.22 | 6.0% | $105.95 |
| Donchian 50-high entry / 20-low exit, equal 1/N | 26 | 46% | 1.10 | 0.8% | -32.2% | 0.23 | 7.1% | $107.13 |
| Buy-and-hold (80 bps entry) | — | — | — | 2.4% | -64.3% | 0.37 | 12.2% | $112.21 |
| Buy-and-hold (zero cost) | — | — | — | 2.4% | -64.3% | 0.37 | 12.7% | $112.66 |
| Cash | 0 | — | — | 0.0% | 0.0% | — | 0.0% | $100.00 |

Same window at **10 bps** and **160 bps** for the primary and the empirically best variant:

| Variant / cost | Trades | Win | PF | CAGR | Max DD | Sharpe | Total return | Final $ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Primary `z_combo_vol25_cap100` @ 10bps | 100 | 17% | 2.55 | 5.0% | -12.3% | 0.55 | 16.6% | $116.64 |
| Primary `z_combo_vol25_cap100` @ 80bps | 100 | 15% | 1.64 | 2.4% | -14.5% | 0.32 | 8.5% | $108.55 |
| Primary `z_combo_vol25_cap100` @ 160bps | 100 | 9% | 1.00 | -0.4% | -18.2% | 0.05 | -0.0% | $99.98 |
| Best-on-test `sma200_vol` @ 10bps | 40 | 35% | 2.94 | 7.6% | -12.4% | 0.55 | 26.4% | $126.44 |
| Best-on-test `sma200_vol` @ 80bps | 40 | 32% | 2.31 | 6.2% | -13.4% | 0.47 | 21.5% | $121.51 |
| Best-on-test `sma200_vol` @ 160bps | 40 | 30% | 1.82 | 4.6% | -14.8% | 0.38 | 16.1% | $116.10 |

### 4.2 Pre-2024 development window (through 31 Dec 2023), 80 bps

This is a split, not a fitted walk-forward: the rules were not tuned on this window. Equity here is sliced from the continuing full-sample book (not a fresh $100).

| Variant | Trades | Win | PF | CAGR | Max DD | Sharpe | Total return | $100 grew to |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Zarattini Combo 25% vol, 100% cap (primary) | 183 | 18% | 6.52 | 16.3% | -14.8% | 1.50 | 162.7% | $262.69 |
| Zarattini Combo 25% vol, 200% cap (paper, not spot-feasible) | 183 | 18% | 6.52 | 16.3% | -14.8% | 1.50 | 162.7% | $262.69 |
| Zarattini Combo equal-weight (no vol target) | 188 | 17% | 5.39 | 60.1% | -40.6% | 1.57 | 1913.5% | $2013.53 |
| Close > SMA 50, 25% vol, 100% cap | 147 | 23% | 3.20 | 27.1% | -20.6% | 1.62 | 361.6% | $461.61 |
| Close > SMA 100, 25% vol, 100% cap | 103 | 21% | 3.73 | 22.5% | -22.3% | 1.35 | 264.6% | $364.63 |
| Close > SMA 200, 25% vol, 100% cap | 46 | 28% | 6.58 | 17.6% | -27.8% | 1.12 | 181.2% | $281.17 |
| Close > SMA 100, equal 1/N (no vol target) | 103 | 20% | 2.39 | 71.1% | -65.1% | 1.27 | 2979.6% | $3079.60 |
| Donchian 20-high entry / 10-low exit, 25% vol | 71 | 54% | 3.43 | 25.3% | -16.8% | 1.63 | 320.5% | $420.54 |
| Donchian 20-high entry / 20-low exit, 25% vol | 54 | 54% | 3.86 | 27.1% | -21.6% | 1.57 | 362.4% | $462.37 |
| Donchian 50-high entry / 10-low exit, 25% vol | 48 | 54% | 4.50 | 21.4% | -15.4% | 1.55 | 243.6% | $343.57 |
| Donchian 50-high entry / 20-low exit, 25% vol | 39 | 49% | 4.23 | 21.9% | -20.8% | 1.46 | 252.7% | $352.73 |
| Donchian 50-high entry / 20-low exit, equal 1/N | 39 | 49% | 3.11 | 83.5% | -57.8% | 1.54 | 4696.4% | $4796.40 |
| Buy-and-hold (80 bps entry) | — | — | — | 73.6% | -87.9% | 1.09 | 3256.0% | $3355.98 |
| Buy-and-hold (zero cost) | — | — | — | 73.6% | -87.9% | 1.09 | 3278.5% | $3378.46 |

### 4.3 Full sample, 80 bps (continuing book)

| Variant | Trades | Win | PF | CAGR | Max DD | Sharpe | Total return | $100 grew to | Yearly folds |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| Zarattini Combo 25% vol, 100% cap (primary) | 280 | 16% | 3.58 | 12.0% | -14.8% | 1.13 | 185.5% | $285.48 | 5/9 positive (2018 -2.1%; 2019 +20.2%; 2020 +36.7%; 2021 +38.0%; 2022 -7.7%; 2023 +28.2%; 2024 +18.4%; 2025 -4.6%; 2026 -3.8%) |
| Zarattini Combo 25% vol, 200% cap (paper, not spot-feasible) | 280 | 16% | 3.58 | 12.0% | -14.8% | 1.13 | 185.5% | $285.48 | 5/9 positive (2018 -2.1%; 2019 +20.2%; 2020 +36.7%; 2021 +38.0%; 2022 -7.7%; 2023 +28.2%; 2024 +18.4%; 2025 -4.6%; 2026 -3.8%) |
| Zarattini Combo equal-weight (no vol target) | 285 | 15% | 2.70 | 40.2% | -40.6% | 1.23 | 2170.5% | $2270.53 | 5/9 positive (2018 -5.1%; 2019 +45.3%; 2020 +104.7%; 2021 +397.1%; 2022 -23.3%; 2023 +87.1%; 2024 +38.6%; 2025 -10.0%; 2026 -9.6%) |
| Close > SMA 50, 25% vol, 100% cap | 232 | 23% | 2.40 | 21.3% | -20.6% | 1.32 | 495.6% | $595.59 | 8/10 positive (2017 +21.9%; 2018 -8.8%; 2019 +23.3%; 2020 +55.4%; 2021 +55.5%; 2022 -14.2%; 2023 +62.6%; 2024 +14.8%; 2025 +5.7%; 2026 +6.3%) |
| Close > SMA 100, 25% vol, 100% cap | 177 | 19% | 2.31 | 17.1% | -22.3% | 1.05 | 330.9% | $430.88 | 8/10 positive (2017 +14.7%; 2018 -7.0%; 2019 +35.0%; 2020 +35.4%; 2021 +51.5%; 2022 -13.8%; 2023 +43.1%; 2024 +15.9%; 2025 +0.1%; 2026 +1.8%) |
| Close > SMA 200, 25% vol, 100% cap | 83 | 28% | 3.70 | 14.2% | -27.8% | 0.90 | 242.3% | $342.30 | 6/9 positive (2018 -8.6%; 2019 +15.7%; 2020 +31.9%; 2021 +39.7%; 2022 -2.1%; 2023 +47.3%; 2024 +27.8%; 2025 -5.1%; 2026 +0.4%) |
| Close > SMA 100, equal 1/N (no vol target) | 177 | 18% | 1.61 | 47.7% | -65.1% | 1.04 | 3579.4% | $3679.41 | 7/10 positive (2017 +68.0%; 2018 -29.1%; 2019 +93.8%; 2020 +98.2%; 2021 +553.2%; 2022 -40.7%; 2023 +73.8%; 2024 +17.4%; 2025 +3.2%; 2026 -1.4%) |
| Donchian 20-high entry / 10-low exit, 25% vol | 121 | 46% | 2.06 | 16.6% | -22.1% | 1.16 | 313.7% | $413.75 | 6/10 positive (2017 +19.6%; 2018 -3.5%; 2019 +18.1%; 2020 +62.9%; 2021 +57.2%; 2022 -12.7%; 2023 +37.9%; 2024 +10.4%; 2025 -7.1%; 2026 -4.1%) |
| Donchian 20-high entry / 20-low exit, 25% vol | 89 | 49% | 2.16 | 18.6% | -22.3% | 1.15 | 383.5% | $483.48 | 6/10 positive (2017 +21.9%; 2018 -10.1%; 2019 +24.2%; 2020 +65.7%; 2021 +67.8%; 2022 -15.3%; 2023 +44.3%; 2024 +15.8%; 2025 -8.1%; 2026 -1.7%) |
| Donchian 50-high entry / 10-low exit, 25% vol | 79 | 53% | 2.77 | 15.0% | -15.4% | 1.19 | 265.2% | $365.21 | 6/10 positive (2017 +18.7%; 2018 -6.7%; 2019 +22.8%; 2020 +39.7%; 2021 +43.9%; 2022 -8.8%; 2023 +37.7%; 2024 +13.0%; 2025 -4.1%; 2026 -1.9%) |
| Donchian 50-high entry / 20-low exit, 25% vol | 62 | 50% | 2.64 | 15.3% | -20.8% | 1.10 | 274.5% | $374.49 | 6/10 positive (2017 +21.0%; 2018 -10.7%; 2019 +26.6%; 2020 +43.5%; 2021 +56.5%; 2022 -13.0%; 2023 +32.1%; 2024 +16.3%; 2025 -3.2%; 2026 -5.7%) |
| Donchian 50-high entry / 20-low exit, equal 1/N | 62 | 48% | 1.94 | 53.2% | -57.8% | 1.22 | 5057.9% | $5157.94 | 6/10 positive (2017 +138.7%; 2018 -37.0%; 2019 +49.2%; 2020 +140.1%; 2021 +647.5%; 2022 -36.6%; 2023 +87.8%; 2024 +33.5%; 2025 -7.0%; 2026 -13.4%) |

### 4.4 2026 year-to-date, 80 bps

| Variant | Trades | Win | PF | Total return | Sharpe | Max DD |
|---|---:|---:|---:|---:|---:|---:|
| Zarattini Combo 25% vol, 100% cap (primary) | 48 | 15% | 0.40 | -3.8% | -0.59 | -6.9% |
| Zarattini Combo 25% vol, 200% cap (paper, not spot-feasible) | 48 | 15% | 0.40 | -3.8% | -0.59 | -6.9% |
| Zarattini Combo equal-weight (no vol target) | 48 | 15% | 0.32 | -9.6% | -0.70 | -15.5% |
| Close > SMA 50, 25% vol, 100% cap | 27 | 19% | 1.54 | 6.3% | 0.53 | -12.3% |
| Close > SMA 100, 25% vol, 100% cap | 15 | 20% | 1.26 | 1.8% | 0.22 | -10.8% |
| Close > SMA 200, 25% vol, 100% cap | 3 | 67% | 1.51 | 0.4% | 0.10 | -12.0% |
| Close > SMA 100, equal 1/N (no vol target) | 15 | 20% | 0.92 | -1.4% | 0.07 | -19.6% |
| Donchian 20-high entry / 10-low exit, 25% vol | 17 | 29% | 0.63 | -4.1% | -0.39 | -10.7% |
| Donchian 20-high entry / 20-low exit, 25% vol | 11 | 36% | 0.84 | -1.7% | -0.06 | -11.8% |
| Donchian 50-high entry / 10-low exit, 25% vol | 10 | 50% | 0.72 | -1.9% | -0.22 | -5.8% |
| Donchian 50-high entry / 20-low exit, 25% vol | 8 | 38% | 0.40 | -5.7% | -0.59 | -8.1% |
| Donchian 50-high entry / 20-low exit, equal 1/N | 8 | 25% | 0.31 | -13.4% | -0.64 | -16.6% |
| Buy-and-hold (zero cost) | — | — | — | -30.9% | -0.53 | -49.8% |

## 5. Promotion bar, all 12, $100 test at 80 bps

| Variant | ≥30 trades | PF ≥1.10 | Majority folds + | Beats cash | Bar |
|---|:---:|:---:|:---:|:---:|:---:|
| Zarattini Combo 25% vol, 100% cap (primary) | yes | yes | no (1/3) | yes | **FAIL** |
| Zarattini Combo 25% vol, 200% cap (paper, not spot-feasible) | yes | yes | no (1/3) | yes | **FAIL** |
| Zarattini Combo equal-weight (no vol target) | yes | yes | no (1/3) | yes | **FAIL** |
| Close > SMA 50, 25% vol, 100% cap | yes | yes | yes (3/3) | yes | **PASS** |
| Close > SMA 100, 25% vol, 100% cap | yes | yes | yes (3/3) | yes | **PASS** |
| Close > SMA 200, 25% vol, 100% cap | yes | yes | yes (2/3) | yes | **PASS** |
| Close > SMA 100, equal 1/N (no vol target) | yes | yes | yes (2/3) | yes | **PASS** |
| Donchian 20-high entry / 10-low exit, 25% vol | yes | no | no (1/3) | no | **FAIL** |
| Donchian 20-high entry / 20-low exit, 25% vol | yes | no | no (1/3) | yes | **FAIL** |
| Donchian 50-high entry / 10-low exit, 25% vol | yes | yes | no (1/3) | yes | **FAIL** |
| Donchian 50-high entry / 20-low exit, 25% vol | no | yes | no (1/3) | yes | **FAIL** |
| Donchian 50-high entry / 20-low exit, equal 1/N | no | no | no (1/3) | yes | **FAIL** |

4 of 12 variants pass the bar at 80 bps on the $100 2024–Nov 2026 test. Variants counted honestly: **12**. No others were run and discarded.

## 6. Permutation test and Kraken robustness

**primary `z_combo_vol25_cap100`**, 1,000 shuffles of each coin’s daily return packages (intraday high/low/open shape kept, serial order destroyed), same 80 bps $100 test window, p-values with a +1 Laplace correction: p(Sharpe ≥ actual 0.32) = 0.732; p(CAGR ≥ actual 2.4%) = 0.718; p(PF ≥ actual 1.64) = 0.592. Valid runs: 1000/1000. Permutation Sharpe median 0.70, 95th percentile 1.64.

**empirically best on test `sma200_vol`**, 1,000 shuffles of each coin’s daily return packages (intraday high/low/open shape kept, serial order destroyed), same 80 bps $100 test window, p-values with a +1 Laplace correction: p(Sharpe ≥ actual 0.47) = 0.717; p(CAGR ≥ actual 6.2%) = 0.641; p(PF ≥ actual 2.31) = 0.331. Valid runs: 1000/1000. Permutation Sharpe median 0.82, 95th percentile 1.80.

**Kraken-price robustness, primary, 80 bps, overlap 2024-11-25 – 2026-11-13** (Binance history used only for warmup, then Kraken USD OHLC): 90 trades, PF 0.32, total return -11.8%, CAGR -5.3%, Sharpe -0.67, max DD -14.5%. This is a shorter window than the full test; it is a price-source check, not a second test period.

## 7. What this means for a $100 Kraken account

**NO-GO.** Do not take this live, even with $100. 
Daily trend following has a real published pedigree, and the BTC-only 10 bps replication is in a plausible range versus the paper, but the version that actually fits this account — three coins, next-open fills, 80 bps, no leverage, $100 — does not earn the promotion bar and/or does not beat sitting in the coins. Kraken minimums are also tight enough that a vol-targeted $100 book will have rejected or skipped rebalances.

Practical constraints, regardless of the verdict:
- Minimum notionals of ~$4–$4 mean you cannot finely vol-target a $100 book. Equal 1/N entries (the `*_eq` variants) are the only sizes that comfortably clear the minimums.
- 80 bps round trip is the *best* Kraken case (maker-maker, no slip). A market ticket at the daily open costs about 160 bps round trip and is the more realistic number. Look at the 160 bps column before treating 80 bps as earned.
- Spot cannot use the paper’s 200% cap. That is a smaller issue than fees, because 25% vol-targeting in crypto usually wants well under 100%.
- This study does not authorize any flag change, restart, or live ticket.

## 8. Method notes

- Variant count: 12. Primary specified first. The “best on test” label, if different, is exploratory.
- Folds are calendar years. The test window has 2024, 2025 and 2026 (through 14 Nov). That is a short fold set; a majority means at least 2 of 3 years positive.
- Trade-level PF uses dollar P&L on the $100 book, including fees attributed to that coin. Open trades at the end of a window are marked to the last open and counted.
- Sharpe and CAGR annualize with 365 days. Drawdown is peak-to-trough of the daily equity curve.
- Buy-and-hold is a static equal-weight book of the coins that are listed at the start of the window, with SOL added and weights reset to 1/3 when it appears. It is not daily-rebalanced (that would be a fee-heavy straw man at 80 bps).
- Permutation shuffles each coin independently. It asks whether the rule’s test-window Sharpe/PF could be luck from the order of daily moves. It is not a full data-mined DSR across other strategies in this program’s earlier research.
- No trading code, flags, `box_env.sh`, logs, or data permissions were changed. No keys were used. No tickets were sent. Raw price zips were cached under `/workspace/edge2-data/daily_trend/` and are not committed.

## 9. Sources

- Zarattini, C., Pagani, A., and Barbon, A. (2025), “Catching Crypto Trends; A Tactical Approach for Bitcoin and Altcoins,” Swiss Finance Institute Research Paper No. 25-80, SSRN 5209907.
- Binance Vision spot daily klines: `https://data.binance.vision/data/spot/monthly/klines/{SYMBOL}/1d/` and daily files for 1–14 Nov 2026.
- Kraken public REST: `/0/public/OHLC?interval=1440` and `/0/public/AssetPairs` (15 Nov 2026).


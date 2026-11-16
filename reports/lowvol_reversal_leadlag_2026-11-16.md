# Low-volume reversal and lead-lag: research check (2026-11-16)

Research only, paper only. No runner was started, and no flags, keys or orders were touched.
Code: `research/edge5/lowvol_leadlag.py` and `research/edge5/robustness.py`. Output: `results.json`, `robustness.json`, `log*.txt`.

## Bottom line

| Angle | Verdict |
|---|---|
| **A. Low-volume short-term reversal** | **Dead as stated.** On VET, HBAR, ALGO and XLM, low-volume drops reverse *less* than high-volume drops at both the 1h and 24h horizons. That matches Campbell-Grossman-Wang (1993) and Conrad-Hameed-Niden (1994): high-volume moves reverse and low-volume moves continue. The crypto "low-volume reversal" papers are about *cross-sectionally illiquid coins* (micro-caps under about $100k weekly volume), not low-volume hours on mid-caps. The one variant that works (24h drop on **high** volume) is just a looser CAPIT, so it isn't a new edge. |
| **B. BTC/ETH → alt lead-lag** | **No tradable edge at 1h.** Leader-to-alt next-hour correlation is about **−0.01** (slightly negative, the "seesaw"), not positive spillover. After big BTC up-hours, alts drift +7 to +17 bps gross over the next 1 to 4h. That's statistically real but below even Binance.US costs, and it's unstable by half-year. It is hopeless at Kraken's 125 to 160 bps. The literature agrees: the exploitable lag lives at 1-minute and sub-second horizons, and it's gone after retail fees. |

## Data and method (pre-specified, no parameter search)

- **Data:** Binance spot 1h klines (data.binance.vision), 2022-01-01 to 2026-11-15 UTC, 42,719 bars per coin. Coins: VET, HBAR, ALGO and XLM, with BTC and ETH as leaders.
- **No look-ahead:** every threshold (return decile, volume median, sigma) uses a rolling window shifted by one bar, so only bars before the signal count. The signal fires on the close of bar t. Entry is at the **open of bar t+1** and exit at the open of bar t+1+H. Each coin holds one position at a time, long only.
- **Costs (round trip):** Binance.US 10 bps, Kraken 125 bps (limit in, market out) and Kraken 160 bps (taker both sides).
- **Tests:** half-year folds; drop-best-5-days; a t-stat clustered by entry hour (all 4 alts often fire on the same hour, so naive trade counts overstate independence); and a joint-date permutation (2,000 random entry-hour draws, preserving cross-coin clustering).
- **Rule count:** this file tests 11 rules. They add to the roughly 530 variants tested earlier in this repo, so treat any single p-value with suspicion.

## Angle A results: volume-conditioned reversal

Rule: the coin's 1h (or 24h) return is at or below the trailing 10th percentile (30-day window for 1h, 90-day for 24h), with quote volume below (or at/above) the trailing median. Buy at the next open and hold 4h (or 24h).

| Rule | Trades | Gross bps/trade | Net @ Binance.US 10 bps | PF @ 10 bps | Net @ Kraken 125 | PF @ 125 | Positive half-years (gross) | Perm p |
|---|---|---|---|---|---|---|---|---|
| 1h drop, **low** vol, hold 4h | 2,546 | +5.1 | −4.9 | 0.93 | −120 | 0.17 | 6/10 | 0.42 (joint-date) |
| 1h drop, **high** vol, hold 4h | 9,216 | +9.5 | −0.5 | 0.99 | −116 | 0.25 | 8/10 | 0.001* |
| 24h drop, **low** vol, hold 24h | 722 | +41.6 | +31.6 | 1.23 | −83 | 0.58 | 8/10 | 0.018 (joint-date) |
| 24h drop, **high** vol, hold 24h | 1,164 | +72.7 | +62.7 | 1.38 | −52 | 0.77 | 9/10 | 0.0005 (joint-date) |

Unconditional drift over the same holds is about 0 bps (−0.1 to −0.9), so the gross numbers are the effect itself. \*Naive per-trade permutation, not clustered.

**Robustness at 10 bps:**
- **24h low-vol:** clustered t = 1.48, not significant. Dropping the best 5 days cuts the result to +8 bps/trade at PF 1.06. Half-years: +69, +66, −8, +67, +85, **−88**, +29, **−22**, +77, +27 bps.
- **24h high-vol:** clustered t = 3.42. Dropping the best 5 days leaves +39 bps at PF 1.23. It was negative in 2022H2 and 2026H1. This is CAPIT's family (a high-volume crash buy) with a looser trigger. It confirms CAPIT's mechanism but adds no new, independent edge.
- **1h low-vol:** clustered t = −2.17. It loses money net of fees.

**What the literature says:**
- **Campbell, Grossman and Wang (1993), QJE:** daily return autocorrelation *falls* with volume. Price drops on high volume reverse; drops on low volume are more likely to be information and persist. [MIT PDF](https://www.mit.edu/~wangj/pap/CampbellGrossmanWang93.pdf) · [NBER w4193](https://www.nber.org/papers/w4193)
- **Conrad, Hameed and Niden (1994), JF 49(4):** weekly returns of high-transaction stocks reverse, while low-transaction stocks show positive autocorrelation (continuation). [RePEc](https://ideas.repec.org/a/bla/jfinan/v49y1994i4p1305-29.html) · [DOI](https://doi.org/10.2307/2329187)
- **Bianchi, Dickerson and Babiak (SSRN 3239670), crypto, 2017 to 2022:** short-term reversal profits (liquidity provision) concentrate in pairs with *lower market activity*. This is a cross-sectional statement about smaller, more volatile, less liquid pairs. [SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3239670)
- **Vojtko et al. (Quantpedia/quantitative.cz, 2023):** the weekly reversal exists only in small, illiquid coins (t = −7.31). Coins under about $100k weekly volume reverse (t = −5.93); liquid coins don't (t = −0.21) and large coins show momentum. The authors warn the reversal "may be driven mostly by low liquidity issues which could make it difficult to realize its reported returns in practice." [PDF](https://quantitative.cz/wp-content/uploads/2023/09/impact_of_size_and_volume_on_cryptocurrency_momentum_and_reversal.pdf)
- **CXO summary of a return-volume double sort:** long low-return/low-volume and short high-return/low-volume, 12h rebalance, gross Sharpe up to 2.96. That's 26 assets from Jan 2017 to May 2018, gross only, and long-short (we can't short on spot). [CXO](https://www.cxoadvisory.com/technical-trading/predicting-crypto-asset-returns-with-past-returns-volume/)
- **Zaremba et al. (2021), "Up or down?", Int. Review of Financial Analysis 78:** last day's return predicts the cross-section negatively, strongest in illiquid coins. [RePEc](https://ideas.repec.org/a/eee/finana/v78y2021ics1057521921002349.html)
- **"Cryptocurrency return reversals" (200 coins, 2015 to 2019):** daily, weekly and monthly reversal, most pronounced among small and illiquid coins. [PDF](https://digitalcommons.fairfield.edu/cgi/viewcontent.cgi?article=1249&context=business-facultypubs)
- **arXiv 2608.21888 (15-minute mean reversion, 183 Binance pairs):** reversal is pervasive and grows with *aggressive taker flow* (a high-activity condition). But the gross edge is about **1.3 bps per trade against a 5 bps round trip**: "too small to clear" costs. [arXiv](https://arxiv.org/html/2608.21888v1)
- **Wen, Bouri, Xu and Zhao (SSRN 4080253):** intraday momentum and reversal in BTC from 2013 to 2020, conditional on jumps, liquidity and FOMC days. It reports a timing-strategy gain, mostly from older, less efficient years. [SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4080253)

**Plain reading:** the "low volume → reversal" idea mixes up two things. *Illiquid coins* reverse, but that's a micro-cap liquidity premium you can't harvest with $100 on thin books after spread. *Low-volume hours* on our mid-caps do not reverse more. The high-volume version is the one with evidence, and we already trade it as CAPIT.

## Angle B results: lead-lag

**Hourly lag correlations (log returns, 2022 to 2026):**

| Pair | Same hour | Leader at t vs alt at t+1 | Alt at t vs leader at t+1 |
|---|---|---|---|
| BTC → VET / HBAR / ALGO / XLM | 0.71 / 0.62 / 0.66 / 0.61 | −0.012 / −0.013 / −0.010 / −0.004 | +0.001 to +0.003 |
| ETH → VET / HBAR / ALGO / XLM | 0.73 / 0.65 / 0.69 / 0.64 | −0.013 / −0.015 / −0.013 / −0.003 | +0.008 to +0.014 |

Alts move *with* BTC in the same hour. The next hour there is a tiny overshoot-correction, not a catch-up.

**Rules** (leader 1h bar return > 2× its trailing 30-day sigma; buy the alt at the next bar's open):

| Rule | Trades (independent hours) | Gross bps | Net @ 10 bps | PF @ 10 | Net @ 125 | Positive half-years (gross) | Clustered t (net 10) | Joint-date perm p (gross) |
|---|---|---|---|---|---|---|---|---|
| BTC up > 2σ, hold 1h | 4,080 | +7.5 | −2.5 | 0.94 | −118 | 8/10 | n/a | 0.001* |
| BTC up > 2σ, hold 4h | 3,576 (894) | +13.8 | +3.8 | 1.05 | −111 | 7/10 | 0.53 | 0.006 |
| ETH up > 2σ, hold 1h | 3,908 | +6.3 | −3.7 | 0.92 | −119 | 8/10 | n/a | 0.001* |
| ETH up > 2σ, hold 4h | 3,412 | +11.6 | +1.6 | 1.02 | −113 | 7/10 | n/a | 0.001* |
| BTC up > 2σ **and alt lagged** (< half BTC's move), hold 1h | 868 | +12.1 | +2.1 | 1.05 | −113 | 6/10 | n/a | 0.001* |
| same, hold 4h | 806 (445) | +16.6 | +6.6 | 1.10 | −108 | 5/10 | 0.96 | 0.007 |
| BTC down > 2σ, long alt, hold 4h (info) | 3,512 | +7.6 | −2.4 | 0.97 | −117 | 7/10 | n/a | 0.013* |

\*Naive per-trade permutation, not clustered.

- **Gross drift is real but tiny.** Per independent event it's about +10 to +15 bps over 4h (joint-date p < 0.01).
- **Net of even 10 bps it disappears.** Clustered t is under 1, and dropping the best 5 days turns both 4h rules negative (−3 bps, PF 0.95 to 0.96).
- **The "lagged alt" version is the most unstable.** Half-years swing from −65 to +60 bps.
- **Kraken fees:** every variant loses about 110 bps per trade.

**What the literature says:**
- **Khan (SSRN 7485359), Binance hourly and 4h, 2024 to 2026, BTC + 9 alts:** same-hour correlation 0.772 vs next-hour **0.006**, with no robust hourly Granger lead after Holm correction. A regime-level effect is real out of sample (5/5 folds) but "returns −0.343% per event after realistic costs." This is the closest match to our test, and it reaches the same answer. [SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7485359)
- **Jia, Wu, Yan and Yin (SSRN 3465924), "Seesaw effect", 22 coins, intraday:** large coins **negatively** predict small coins' next returns. Our −0.01 next-hour correlation is consistent with this. [SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3465924)
- **Asia-Pacific Financial Markets (2026), "Price transmission from Bitcoin to altcoins":** BTC → small-cap alt delay exists at **1-minute** bars and is larger for low-trade-count alts. The strategy uses ML, and the abstract reports no net-of-cost retail numbers. [Springer](https://link.springer.com/article/10.1007/s10690-026-09589-z)
- **Frontiers in Blockchain (2026), minute-level microstructure across 6 coins on Binance:** signals carry real but weak information, and "no strategy survives realistic exchange fees." Net Sharpe is deeply negative at 10 bps a side. [Frontiers](https://www.frontiersin.org/journals/blockchain/articles/10.3389/fbloc.2026.1811716/full)
- **arXiv 2108.09750, cross-exchange BTC lead-lag at under 1 second:** strong predictability (R² 10 to 37% at 500 ms), but taker PnL is negative at normal fee tiers. Only a maker strategy with top-tier fees worked live. [arXiv](https://arxiv.org/pdf/2108.09750)
- **arXiv 2201.02040:** significant lead-lag graphs at 1 and 5 minutes. [arXiv](https://arxiv.org/html/2201.02040v2)
- **Hedonist Research, transfer-entropy lead-lag (40 coins):** "KILLED". Lag is 1 to 2 bars, the direction flips out of sample, and it's net negative after cost. This is a practitioner blog, so it's weak evidence, but it agrees. [Link](https://intel.hedonist.trading/research/transfer-entropy-lead-lag-network/)
- **JMU VAR study, 2015 to 2020 hourly (includes XLM):** BTC Granger-causes alts more after 2017, and the hourly impulse turns *negative* by hours 2 to 4. That's consistent with our seesaw sign. [PDF](https://commons.lib.jmu.edu/cgi/viewcontent.cgi?article=1074&context=jmurj)

## Costs on a US exchange

- **Binance.US:** under 10 bps round trip, assuming low-tier taker plus a little spread. The repo's plan doc uses about 0% maker buy and 0.02% market sell. VET, HBAR, ALGO and XLM have thin Binance.US books, so real spread and slippage can add 5 to 20 bps. That alone kills every Angle B variant and the 1h Angle A rules.
- **Kraken:** 125 to 160 bps round trip. **Nothing in this file survives Kraken fees**, including the 24h high-volume rule (−52 bps/trade).

## What a narrow paper test would need (if we did one at all)

- **Angle A:** don't paper-test the low-volume version; it fails in-sample. The high-volume 24h version is already covered by the CAPIT / CAPIT-24L paper track. At most, log it as a looser CAPIT shadow variant from the existing 1h Binance.US feed; no new runner is needed. Confirmation bar: 30+ closed trades, PF ≥ 1.10 net, and beating CAPIT on the same dates. At about 240 trades a year across 4 coins (correlated), that's roughly 2 months of trades but **9 to 12 months for independent regimes**.
- **Angle B:** any honest test needs **1-minute or tick data** (Binance.US websocket, both BTC and the alt), a sub-second runner, and maker-only execution, because the edge the literature finds lives at 1 minute or faster. Hourly paper testing would only re-confirm a gross +10 bps that fees eat. It would also need about 300+ independent events, roughly 3 to 4 months of 1-minute logging, before any fill-quality verdict. **Recommendation: don't build it.** Retail fee tiers can't harvest it, and that's exactly what the costed papers find.

## Caveats

- Binance global prices stood in for Binance.US, which has thinner books and worse fills.
- These are hourly bars only. 4h and 24h holds overlap in time across coins, which is handled by clustering by entry hour.
- The 11 rules here add to the roughly 530 earlier variants.

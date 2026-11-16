# Strategy proposal: CAPIT-24L (crash-buy with a limit entry), 2026-11-16

**Research and paper only.** No live flags changed, no keys, no orders. The paper runner and the shadow loop were not touched.
New code: `research/edge4/` (`refine.py`, `book4.py`, `extra_limit_nor2.py`; results `*.json`, logs `log_*.txt`).

## TL;DR (blunt)

- There is **one** idea in our data with a real, repeatable gross effect: buy liquid coins after a volatility-normalized
  24h crash on heavy volume, hold about 2 days (CAPIT-24). The literature agrees that extreme drops in crypto partly
  overshoot, and that the bounce is biggest on the biggest forced-selling flushes.
- Every "smarter" add-on we tried (OI flush, funding filters, regime filter, stops, shorter holds) either does nothing
  robust, helps on one universe and hurts on the other, or cuts the trade count below anything testable.
- The one change that helps **on both universes, for a reason that is not curve-fitting**, is execution: **enter with a
  post-only limit 0.5% under the signal close instead of a market order.** On Kraken tier 1 that swaps an 80 bps taker
  fee for a 40 bps maker fee and buys slightly lower.
- Deflated Sharpe is still nowhere near 0.95 (0.00 to 0.22) after ~529 variants tried across all rounds. **This is not
  proven. It is the best-supported thing we have, and it should be confirmed on forward paper before a dollar goes live.**
- It is rare: about 12-14 trades a year on the 4-coin book. Reaching 30 forward trades on that book alone takes ~2 years,
  so the confirmation plan pools the 4-coin book with the 12-coin shadow (about 35-45 trades a year in total).

## 1. Edge hypothesis (why it should exist)

1. **Forced selling overshoots.** A crash that is large relative to the coin's own volatility *and* comes with a volume
   spike is disproportionately liquidations, margin calls and stop cascades: sellers who must sell regardless of price.
   Liquidity providers widen out and depth disappears (Lim 2026; arXiv 2608.03616: impact spikes 1.2-9x in every cascade,
   ~88% of forced selling lands in the first 30 minutes). When the forced flow stops, price partly recovers.
2. **The academic evidence points the same way, with caveats.** Overreaction after extreme negative moves is documented
   for crypto ("A broad analysis of short-term overreactions", 50 coins;
   magnitude effect: bigger drop, bigger inefficiency). Independent practitioner work (VYX) finds only the most extreme
   flushes bounce by more than costs. Wen et al. (2022) find intraday *reversal* in crypto outside US equity hours,
   linked to overreaction.
3. **Why it is not arbitraged away:** the edge needs capital to step in during the worst hours, and the trade is
   unpleasant (worst MAE in our 12-coin test was -59% intrabar during the Oct 10 2025 flash crash). Small, patient,
   unlevered spot money can carry that; leveraged money cannot.
4. **What argues against it:** large liquid coins show short-term *momentum* at weekly horizons (Dobrynskaya-style
   size/volume study), and extreme negative funding on down days predicts *continuation* (Shek 2026, SSRN). Our edge
   lives in a narrow 24-48h window after a very specific event. Outside it, the market does not mean-revert reliably.

## 2. Exact rules

**Universe (traded in paper):** VET, HBAR, ALGO, XLM, USD pairs (all listed on Kraken and Binance.US; this is the book the
paper runner already uses). **Shadow (signal-only, counted as evidence):** BTC ETH SOL XRP DOGE ADA AVAX LINK LTC BNB DOT TRX
(already logged by the capit24 shadow loop).

**Signal** (unchanged frozen CAPIT-24, computed on each CLOSED 1h bar):
- `z24 = ln(close_t / close_{t-24}) / (stdev of 1h log returns over the prior 720 bars × √24)` ≤ **−2.5**
- **and** 24h quote volume ≥ **2.0×** its trailing 30-day average 24h volume.

**Entry (the change):** at bar close, place a **post-only limit buy at 0.995 × signal-bar close**, good for **60 minutes**
(the next 1h bar). If the next bar opens below the limit, the fill is at the open. If not filled in 60 minutes, cancel and
skip the signal. Historically ~85% of signals filled (58 of 68 on the 4-coin book).

**Exit:** time stop, **48h** after the entry bar's open, market (taker) sell. No profit target.
No per-trade price stop: a −15% stop was tested and **lowered** PF (12-coin R2 book: 1.33 → 1.18 at 125 bps) because
crash-buys often dip further before bouncing.

**Logged but not traded:** the BTC regime label (R2: BTC daily EMA200 slope + ADX, "bear" = skip). It improved the 12 majors
a lot and slightly hurt the 4-coin book, so it is a *shadow field* to decide at the 30-trade review, not a rule now.

## 3. Sizing and risk for $100

| Parameter | Value |
|---|---|
| Slots | **2 × $50**, one per coin, ties go to the most negative z |
| Max exposure | $100 (100%); no leverage, no margin, no futures, spot long only |
| Kraken minimum order | ~$3.3-4.4 on these four (fine at $50) |
| Per-trade loss | No price stop. Historical worst trade −13.3% (4 coins) / −12.8% (12 coins) = about −$6.70 on a $50 slot |
| Book halt (K3) | Equity ≤ **$70** (−30%): stop new entries, review. Backtest max DD at 50% slots: 15-18% (4 coins), 37-48% (12 coins) |
| Daily cap | Structural: at most 2 entries open at any time, so at most $100 at risk on one crash day; no averaging down, no re-entry in a coin while it has an open lot |
| Kill K1 | PF < 1.10 after 30 closed trades, at the fee model of the venue that would actually be used |
| Kill K2 | Two consecutive negative quarters while the book is below cash |
| Kill K4 | First 15 forward trades average below −100 bps net |

## 4. Fees (round trip, this is the whole game)

| Venue / execution | Round trip | Source |
|---|---|---|
| Binance.US, taker + 3 bps slip each side | **10 bps** | `reports/venue_fees_2026-11-15.md` |
| Kraken Pro tier 1, maker both sides (best case) | 80 bps | Kraken fee schedule: 0.40% maker / 0.80% taker |
| **Kraken tier 1, limit entry (maker 40) + market exit (taker 80 + 5 slip)** | **125 bps**: the model this proposal uses | same |
| Kraken tier 1, market both sides | 170 bps | same |

A $100 account will be tier 1 indefinitely (tier 2 needs $2.5k/30-day volume). Binance.US is ~12x cheaper; if it is
usable for the user, it is the better venue for the same rule.

## 5. Evidence

Notation: PF and mean are after costs; "Q+" = positive quarters (empty quarters count as not positive); DSR = deflated
Sharpe, day-clustered, N = 527-529 cumulative variants; perm p = 1,000 random-entry runs (0.001 is the floor).

**A. The 4-coin paper book** (VET/HBAR/ALGO/XLM, Binance Vision 1h, 2022-08 → 2026-11-15, 2 × 50% slots)

| Variant | Trades | PF @125 | Mean @125 | Q+ @125 | PF @170 | DSR @125 | 2024 / 2025 / 2026 bps per trade @125 | Net ex best 5 days |
|---|---|---|---|---|---|---|---|---|
| Frozen, market entry | 68 | 2.50 | +296 | 10/18 | 2.17 | 0.13 | +274 / +996 / +260 | +3,228 bps |
| **Frozen, limit entry (proposal)** | **58** | **2.47** | **+310** | **10/18** | 2.16 | **0.22** | **+347 / +844 / +274** | **+2,180 bps** |
| + BTC R2 filter, market | 56 | 2.32 | +288 | 8/18 | 2.03 | 0.13 | +274 / +1067 / +160 | −752 bps |
| + BTC R2 filter, limit | 48 | 2.28 | +293 | 8/18 | 2.00 | 0.18 | +347 / +906 / +72 | −1,723 bps |

Apples to apples (what you would really pay on Kraken): market entry at 170 bps PF **2.17** vs limit entry at 125 bps PF
**2.47**. Book: +90% non-compounded on 50% slots, max drawdown 15% at 125 bps. Last 12 months: 12 trades, +260 bps
average. Per coin at 125 bps: ALGO 18 tr +533, VET 12 tr +418, XLM 13 tr +304, **HBAR 15 tr −40** (HBAR does not carry its weight).
2022 (4 trades, −563) and 2023 (10 trades, −70) were losing years.

**B. The 12 majors** (dev 2023-02 → 2026-09, holdout Oct 1 - Nov 13 2026)

| Variant | Trades | PF @80 | PF @125 | Mean @125 | Q+ @125 | PF @170 | DSR @125 | 2026 bps/tr @125 | Net ex best 5 days @125 | Perm p |
|---|---|---|---|---|---|---|---|---|---|---|
| V0 frozen, market | 125 | 1.23 | 1.04 | +9 | 9/15 | **0.87** | 0.00 | −328 (23 tr) | — | 0.011 |
| V9 frozen, limit | 108 | 1.30 | 1.08 | +21 | 10/15 | 0.91 | 0.00 | −264 (22 tr) | −8,676 | 0.013 |
| V1 R2, market | 103 | 1.58 | 1.33 | +76 | 9/15 | 1.12 | 0.00 | −26 (7 tr) | −2,953 | 0.001 |
| V2 R2, hold 24h | 111 | 1.60 | 1.28 | +50 | 9/15 | 1.02 | 0.00 | +19 | — | 0.001 |
| V3 R2, hold 12h | 146 | 1.16 | **0.90** | −19 | 7/15 | 0.69 | 0.00 | +1 | — | 0.001 |
| V4 R2, −15% stop | 109 | 1.37 | 1.18 | +51 | 9/15 | 1.02 | 0.00 | −26 | — | 0.002 |
| **V5 R2, limit entry** | 87 | 1.70 | **1.43** | +93 | 10/15 | 1.20 | 0.004 | +146 (6 tr) | −2,806 | 0.002 |
| V6 R2, skip extreme-low funding | 95 | 1.89 | 1.61 | +133 | 9/15 | 1.37 | 0.001 | −273 (4 tr) | — | 0.001 |
| V7 R2, require extreme-low funding | 40 | 2.18 | 1.81 | +142 | 10/15 | 1.50 | 0.029 | +305 (3 tr) | — | 0.005 |
| V8 R2, 15m green-bar confirmation | 102 | 1.76 | 1.49 | +111 | 10/15 | 1.27 | 0.001 | 0 | **−400** | 0.001 |

Holdout (Oct 1 - Nov 13 2026): 2 trades for every variant (BNB, LINK on the Oct 29-30 dump), positive, statistically meaningless.

**What this says:**
- **Limit entry helps everywhere when costs are compared honestly** (12 majors: 0.87 → 1.08 without R2, 1.12 → 1.43 with R2;
  4 coins: 2.17 → 2.47). That is the only robust improvement, and it is motivated by fees, not by fitting.
- **On the 12 majors the frozen rule does not survive Kraken costs** without the BTC filter (2026 is −264 to −328 bps per
  trade). The majors only work with R2, and even then only ~6-7 trades in 2026.
- **Concentration:** on the 12 majors, removing the 5 best crash days turns every variant's net sum negative. On the 4-coin
  book it stays positive (+2,180 bps). The edge is "a handful of crash days a year", not a steady drip.
- **Shorter holds lose** after Kraken fees (12h: PF 0.90). The 15m/30m windows are too short to clear 125 bps. 1h signal,
  48h hold is the shortest window that works.
- **Funding filters contradict each other.** Skipping extreme-low funding (the Shek 2026 continuation finding) and *requiring*
  it (the liquidation-flush reading) both raise PF on tiny, unstable samples. Not adopted.
- **OI flush** (earlier today, `reports/oiflush_lookahead_fix_2026-11-16.md`): it is mostly plain CAPIT, and DSR ≤ 0.19. Not adopted.
- **Against the promotion bar (proposal = 4-coin, limit, 125 bps):** ≥30 trades ✅ (58); PF ≥ 1.10 ✅ (2.47); majority of
  quarters positive ✅ barely (10/18); permutation p < 0.01 ✅ on the 12-coin analogue (0.002), not run on 4 coins;
  beats buy-and-hold: ✅ in 2025, 2026 and the holdout (the basket fell), ❌ over the 2023-24 bull; regime split: strongest
  in bull/bear trends, weakest in flat (12-coin R2 limit: flat PF 0.90 at 80 bps); **deflated Sharpe ❌ (0.22 vs 0.95).**

## 6. Honest weaknesses

1. **Multiple testing.** ~529 variants have now been tried across all rounds. DSR ≤ 0.22 means the historical Sharpe is
   not distinguishable from the best of many random tries. Only forward data can fix that.
2. **The 4 coins were chosen in the Nov 15 study after looking at CAPIT results** on 8 candidates. Selection bias is real.
   HBAR is already a laggard.
3. **Limit-fill model is optimistic:** it assumes a fill whenever the bar's low touches the limit (no queue position, no
   partial fills). Forward paper must record whether the real Kraken book traded *through* the limit.
4. **Rare and lumpy:** ~12-14 trades a year on 4 coins; most profit comes from a few crash days; 2022-23 lost money.
5. **Fills used Binance Vision USDT prices**, not Kraken USD prints. VET only listed on Kraken in Jan 2026; its Kraken
   book is thin (~$100k/day).
6. **No stop means real intratrade pain.** −59% intrabar MAE has happened on a major (LINK, Oct 10 2025).
7. **The box hibernates.** A missed 60-minute limit window means a missed trade. Live needs an always-on host.

## 7. Paper evidence that would confirm or kill it

- **Run:** add a limit-entry track to the existing CAPIT paper runner: same signal, post-only limit at 0.995 × close for
  60 minutes, record "touched", "traded through" and the fill price, and price it at **125 bps** (Kraken) next to the existing
  10 bps (Binance.US). The 12-coin shadow loop gets the same limit-entry fields. The live flags stay locked.
- **Confirm (eligible to ask the user for a $100 live OK):** ≥30 closed forward trades pooled across the 4-coin book and
  the 12-coin shadow, with the 4-coin book itself ≥ 12 trades; PF ≥ 1.30 at 125 bps; mean ≥ +75 bps; a majority of
  forward quarters positive; fill rate ≥ 70% with "traded through" (not just touched); book drawdown < 30%.
  Expected time: roughly 9-12 months at historical signal rates. There is no honest way to make it faster.
- **Kill:** K1 PF < 1.10 at 30 trades; K2 two negative quarters below cash; K3 equity ≤ $70; K4 first 15 trades < −100 bps;
  or a fill rate < 50% (the limit-entry premise fails).
- **Decide at the 30-trade review:** whether to drop HBAR, and whether to switch on the R2 BTC filter (it is logged, not traded).

## Sources

Academic / research:
- Wen, Bouri, Xu, Zhao (2022), *Intraday return predictability in the cryptocurrency markets: momentum, reversal, or both*,
  NAJEF: https://doi.org/10.1016/j.najef.2022.101733 (PDF: https://www.cuats.co.uk/wp-content/uploads/2026/02/ssrn-4080253.pdf)
- Shen, Urquhart, Wang (2022), *Bitcoin intraday time-series momentum*, Financial Review: https://onlinelibrary.wiley.com/doi/10.1111/fire.12290
- *A broad analysis of short-term overreactions in the market for cryptocurrencies* (50 coins, 2015-2020): https://d-nb.info/1266491473/34
- *Do cryptocurrencies overreact? Evidence following extreme one-day price movements*: https://sigarra.up.pt/fep/en/pub_geral.show_file?pi_doc_id=353747
- *Impact of size and volume on cryptocurrency momentum and reversal*: https://www.quantitative.cz/wp-content/uploads/2023/09/impact_of_size_and_volume_on_cryptocurrency_momentum_and_reversal.pdf
- Lim, *Anatomy of a Crypto Cascade: Minute-Level Evidence from the October 2025 Crash* (SSRN): https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6579278
- Lim, *Two-Regime Liquidity Recovery After a Perpetual Futures Liquidation Cascade* (SSRN): https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6636998
- *Measuring the engine of a liquidation cascade* (arXiv 2608.03616): https://arxiv.org/html/2608.03616v1
- Ali, *Anatomy of the Oct 10-11, 2025 Crypto Liquidation Cascade* (SSRN): https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5611392
- Shek, *Conditional Arbitrage Capacity: Evidence from Crypto Perpetual Funding Rates* (SSRN): https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7520320
- Memon et al. (2026), *Extreme Perpetual Futures Funding and Subsequent Bitcoin Returns*: https://www.aijmr.com/research-paper.php?id=1493
- Presto Research, *Can Funding Rate Predict Price Change?*: https://www.prestolabs.io/research/can-funding-rate-predict-price-change
- Hansen et al., *Periodicity in Cryptocurrency Volatility and Liquidity* (J. Fin. Econometrics): https://doi.org/10.1093/jjfinec/nbac034
- *The crypto world trades at tea time* (RQFA 2024): https://link.springer.com/article/10.1007/s11156-024-01304-1
- Zarattini, Pagani, Barbon, *Catching Crypto Trends* (SSRN): https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5209907
- Bailey & López de Prado, *The Deflated Sharpe Ratio* (method used for DSR).

Practitioner:
- VYX, *Does Crypto Bounce After a Liquidation Cascade?*: https://vyx.app/research/does-crypto-bounce-after-a-liquidation-cascade

Fees:
- Kraken Pro fee schedule: https://www.kraken.com/pro/fees
- Kraken cross-platform fee tiers (updated Oct 30 2026): https://support.kraken.com/articles/cross-platform-fee-tier-changes

Internal: `reports/edge_search_2026-11-14.md`, `reports/oiflush_lookahead_fix_2026-11-16.md`,
`reports/low_price_coins_study_2026-11-15.md`, `reports/venue_fees_2026-11-15.md`, `research/edge2`, `research/edge3`, `research/edge4`.

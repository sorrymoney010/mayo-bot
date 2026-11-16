# Low unit-price coins: PUMP and established peers

November 15, 2026, America/Chicago · research only · no orders, no keys, no live flags, no process restarts

Prices and backtests use complete UTC daily bars through **15 Nov 2026**. Incomplete 16 Nov 2026 UTC bars were dropped.

**Verdict: do not put the $100 on PUMP, and do not pick coins because the unit price looks cheap.** A low print ($0.003 vs $1) is almost always a large token supply, not a discount and not an edge. On this set, unit price did not predict returns, crash-buy results, or trend results.

What the data *does* support is the same CAPIT-24 crash-buy rule we already had, applied to liquid established names — not because they are cheap, and not on PUMP itself (only **2** signals in 14 months of hourly data). A four-coin paper basket of VET, HBAR, ALGO and XLM, 2022-01-01 to 2026-11-15, max two 50% slots, **78 trades**, Kraken maker 80 bps round trip: profit factor **2.32**, **13/20** quarterly folds positive, mean net **+292 bps** per trade. That clears our promotion bar on paper at maker fees (and at Binance.US 10 bps). It **does not** clear the fold-majority bar at Kraken taker 160 bps (10/20). This is a paper test, not a live go. PUMP is not in that basket.

Trend (daily close > SMA 100) and a pre-specified 5-day mean-reversion rule are no-gos after Kraken fees, especially in the last 13 months, when buy-and-hold on every name in the set was down 27–75%.

## 1. Direct answer

- **Low unit price is not an advantage.** $100 of PUMP at $0.00297 and $100 of ATOM at $1.31 are the same dollars of exposure. Kraken’s minimum fill on 15 Nov 2026 is about **$3.26–$4.60** on all eight names in this study, because `ordermin` scales inversely with price. Spearman rank correlation, n=8, unit price vs CAPIT mean net in the common window: **−0.14** (noise). vs buy-and-hold net: **+0.19**. The higher-priced control (ATOM) still had a long-sample CAPIT profit factor of 1.81 after 80 bps.
- **PUMP (the pump.fun official token) is a real, exchange-listed coin, not a bonding-curve meme.** It is also a bad $100 buy-and-hold and has no usable crash-buy sample of its own. From the 14 Jul 2025 Kraken open ($0.006201) to the 15 Nov 2026 close ($0.002974): **−52.0%** gross, **−52.8%** after 80 bps, max drawdown **−86.1%**. Versus the $0.004 ICO: **−25.6%**. Versus the $0.009 daily high on 14 Sep 2025: **−67.0%**. CAPIT-24 fired **twice** (30 Apr and 5 Jun 2026). That is not a test.
- **The only rule here with evidence is CAPIT-24**, the same 24-hour vol-normalized crash + 2× volume, 48-hour hold, already frozen in `research/edge2/candidate.py`. It shows up on VET, ANKR, HBAR, ALGO and XLM over 2022–2026 after 80 bps, and it is weaker — not absent — on ATOM. It is not a “cheap coin” effect.
- **Do not go live.** Per-coin trade counts are 16–29 (bar is 30). The 78-trade basket result is a paper simulation on Binance Vision USDT hours, not Kraken fills. VET only listed on Kraken in January 2026. If a paper test is run, Binance.US is still the fee venue (0% maker / 0.02% taker; 10 bps with a small slip assumption), not Kraken.

## 2. PUMP history and stats

PUMP is the official token of pump.fun, the Solana memecoin launchpad. The platform is a real business with large historical fee revenue; that does not make the token a bargain at a sub-cent print.

| Fact | Figure | Source |
|---|---|---|
| ICO | 12 Jul 2025, 14:00 UTC; $0.004 per token; $4B fully diluted; 150B tokens offered (15% of 1T total supply); no lock-up | [Kraken sale details](https://support.kraken.com/articles/pump-sale-details) |
| Sale | Sold out in about 12 minutes; ~$500M on-chain plus ~$100M via CEXs, often cited as a $600M raise | [Blockworks, 14 Jul 2025](https://blockworks.com/news/pump-fun-500m-ico-twitch) |
| Kraken listing | Spot and margin live 14 Jul 2025, 14:00 UTC | [Kraken blog](https://blog.kraken.com/product/asset-listings/pump-is-available-for-trading) |
| US persons | Excluded from the ICO (Regulation S). The token later trades on Kraken and Binance.US | Kraken sale details |
| First Kraken day (14 Jul 2025) | Open $0.006201, low $0.004445, close $0.005737 (about 43% above ICO) | Kraken public OHLC |
| Daily high (ATH in this series) | **$0.009** on 14 Sep 2025; highest daily close **$0.008612** on 15 Sep 2025 | Kraken public OHLC (Kraken’s public price page quotes an ATH near $0.0088) |
| Daily low | **$0.001151** on 25 Jun 2026, close $0.001197 | Kraken public OHLC |
| 15 Nov 2026 close | **$0.002974** | Kraken public OHLC |
| vs ICO / vs listing open / vs $0.009 high | −25.6% / −52.0% / −67.0% | computed |
| 2025 (listing close → 31 Dec close $0.001911) | **−66.7%** | computed |
| 2026 YTD (2 Jan open $0.00192 → 15 Nov close) | **+54.9%** (a bounce off the June low, still well below ICO and listing) | computed |
| Realized vol (Kraken daily, annualized) | **135%**, vs ~73–90% on VET, ANKR, HBAR, ALGO, XLM, ATOM in their Kraken windows | computed |
| Circulating / market cap (Kraken public page, mid-Nov 2026) | ~458B circulating (~56% of 823B quoted); market cap ~$1.35B; FDV ~$2.4B | [Kraken PUMP price page](https://www.kraken.com/prices/pump) |
| Launch-day context | First-day range roughly $0.0049–$0.0061 in contemporary press; ~22% of ICO buyers sold on DEX the first day | [Decrypt, 15 Jul 2025](https://decrypt.co/330222/pump-fun-token-600m-ico-bonk) |

Hourly data is shorter than the Kraken daily file. Binance Vision and Binance.US `PUMPUSDT` start **11 Sep 2025** and **10 Sep 2025**, about two months after the Kraken listing. CAPIT-24 on PUMP was run only on that hourly window (plus a 30-day warmup), which is why the two trades both fall in 2026.

“Verified” here means: listed on a major exchange, mint of the official project token, public ICO and listing record. It does **not** mean audited cash flows to holders, a completed unlock schedule, or a positive expected return. The ICO had **no vesting**. A sub-cent price is the 1-trillion supply, not a safety label.

## 3. Universe, venues, and $100 costs

Pre-registered set, not mined from the 200+ Kraken names under $0.01. **Near-PUMP** (~$0.003): PUMP, VET, ANKR, JASMY. **User-named / mid** (~$0.06–$0.14): HBAR, ALGO, XLM. **Higher-price control:** ATOM (~$1.31). Memes (BONK, PEPE, SHIB, FLOKI, and the rest) were excluded on purpose. Gaming/thin names in the same price band (GALA, CSPR, XCN, VELO, CELR, and similar) were left out of the backtest; they are not needed to answer the unit-price question.

Kraken and Binance.US public APIs, 15 Nov 2026:

| Coin | 15 Nov Kraken close | Kraken USD pair | `ordermin` | Min notional | Kraken OHLC from | Binance.US | 24h Kraken USD volume (live, 15 Nov evening CT) |
|---|---:|---|---:|---:|---|---|---:|
| PUMP | $0.002974 | PUMP/USD | 1,200 | **$3.57** | 14 Jul 2025 | PUMPUSDT only, from 10 Sep 2025 | ~$1.4M |
| VET | $0.004930 | VET/USD | 900 | **$4.44** | **2 Jan 2026** (new Kraken listing) | VETUSD and VETUSDT trading | ~$98k |
| ANKR | $0.003240 | ANKR/USD | 1,300 | **$4.21** | 26 Nov 2024 (720-bar cap) | ANKRUSDT only (ANKRUSD is BREAK) | ~$32k (thinnest) |
| JASMY | $0.003280 | JASMY/USD | 1,300 | **$4.26** | 26 Nov 2024 (720-bar cap) | **BREAK** on USD and USDT | ~$113k |
| HBAR | $0.05930 | HBAR/USD | 55 | **$3.26** | 10 Jul 2025 | HBARUSD and HBARUSDT trading | ~$1.3M |
| ALGO | $0.08004 | ALGO/USD | 41 | **$3.28** | 26 Nov 2024 (720-bar cap) | ALGOUSD and ALGOUSDT trading | ~$1.4M |
| XLM | $0.141597 | XLM/USD | 25 | **$3.54** | 26 Nov 2024 (720-bar cap) | XLMUSD and XLMUSDT trading | ~$4.3M |
| ATOM | $1.3129 | ATOM/USD | 3.5 | **$4.60** | 26 Nov 2024 (720-bar cap) | ATOMUSD and ATOMUSDT trading | ~$196k |

All eight clear a $100 ticket. A $100 book is not blocked by “too few coins” the way a vol-targeted BTC/ETH/SOL trend book was. ANKR’s Kraken book is the one to treat as slip-risky. JASMY cannot be paper-tested on Binance.US right now.

**Fees used (round trip), same model as the earlier research:**

| Label | Round trip | Meaning |
|---|---:|---|
| `bus_maker` | 0 bps | Binance.US 0% maker, no slip (best case) |
| `bus_taker` | 4 bps | Binance.US 0.02% taker each way, no slip |
| `bus_base` | 10 bps | 0.02% taker + 3 bps slip each way (prior research default) |
| `krk_maker` | 80 bps | Kraken tier-1 0.40% maker each way, no slip (best case) |
| `krk_taker` | 160 bps | Kraken tier-1 0.80% taker each way, no extra slip (the user’s figure) |

Binance.US schedule: [fee page](https://www.binance.us/fees) and the [22 Apr 2026 announcement](https://blog.binance.us/zero-fee-trading/) of 0% maker / 0.02% taker on standard pairs. On a $100 round trip that is **$0.00–$0.10** versus **$0.80–$1.60** on Kraken. The gap is the same whether the coin prints at $0.003 or $1. Unit price does not change the fee bill.

## 4. Data and rules (pre-specified, not tuned)

- **Daily prices:** Kraken public OHLC `interval=1440` (max 720 bars — that is why several Kraken series start 26 Nov 2024). Binance Vision public daily klines for the long history (VET from Jul 2018, etc.). PUMP’s complete listing history is the Kraken daily file (14 Jul 2025 onward).
- **Hourly prices (CAPIT):** Binance Vision `1h` from 1 Jan 2022 (PUMP from 11 Sep 2025). `api.binance.com` returns HTTP 451 from this host; Vision and Binance.US both responded. Kraken hourly only returns the last ~720 hours, so it cannot support a multi-year hourly test.
- **Signals at bar close, fills at the next open.** Non-overlapping per coin. Incomplete last bars dropped. No look-ahead in the CAPIT sigma (`.shift(1)` on the 720-hour standard deviation, same as the frozen candidate).

Rules, frozen before looking at these coins:

1. **CAPIT-24** (exact `candidate.py`): on each closed 1-hour bar, 24-hour log return / (trailing 720-hour 1-hour sigma × √24) ≤ −2.5, and 24-hour quote volume ≥ 2× the trailing 30-day average 24-hour volume. Buy next hour’s open, sell 48 hours later. Optional **R1**: BTC’s previous daily close above its 200-day SMA.
2. **Trend SMA100:** long when yesterday’s close is above the 100-day SMA, exit when it is not, fill next open.
3. **Mean reversion MR5:** 5-day log return / (20-day sigma × √5) ≤ −2, buy next open, hold 5 days.
4. **Buy-and-hold** on the same windows, one entry cost.

Permutation tests: 1,000 circular shifts of the CAPIT/MR signal (preserving clumpiness), and 1,000 shuffled-return reconstructions for SMA100. One-sided p = (beats + 1) / (1001). The promotion bar we already use: ≥30 trades, PF ≥ 1.10 after fees, majority of quarterly folds positive, beats cash; ideally permutation p < 0.01 and beats buy-and-hold.

## 5. Backtest results

### 5.1 Buy-and-hold (the base rate)

These names were mostly a down trade. Holding “because it’s cheap” lost money in every common window.

| Coin | Window | Gross | Net, 80 bps | Max DD |
|---|---|---:|---:|---:|
| PUMP | Kraken listing 14 Jul 2025 – 15 Nov 2026 | −52.0% | −52.8% | −86.1% |
| PUMP | Common, 15 Oct 2025 – 15 Nov 2026 | −27.5% | −28.3% | −77.0% |
| VET | Common | −74.3% | −75.1% | −76.1% |
| ANKR | Common | −71.6% | −72.4% | −70.4% |
| JASMY | Common | −69.7% | −70.5% | −71.0% |
| HBAR | Common | −68.8% | −69.6% | −72.0% |
| ALGO | Common | −61.0% | −61.8% | −60.6% |
| XLM | Common | −58.0% | −58.8% | −59.0% |
| ATOM | Common | −62.2% | −63.0% | −63.3% |
| HBAR | Vision full, 29 Sep 2019 – 15 Nov 2026 | **+91.2%** | +90.4% | −92.8% |

HBAR is the only name that is up over its full Vision history, and that path still had a 93% drawdown. JASMY and ALGO are down more than 97% from their Vision first opens. Full-history buy-and-hold is not an argument for a $100 account.

### 5.2 CAPIT-24, per coin, hourly 2022–15 Nov 2026 (PUMP from late 2025)

Net of **Kraken 80 bps** unless noted. Permutation is on this long window.

| Coin | Trades | Win | PF (80 bps) | Mean net | Folds + | PF (160 bps) | Perm p (80 bps) | $100 compounded, 80 bps | Bar |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| PUMP | **2** | 50% | 3.98 | +129 bps | 1/1 | 1.59 | n/a (<5 trades) | $102.55 | **FAIL** (n) |
| VET | 26 | 65% | 2.59 | +353 bps | 10/19 | 2.09 | **0.001** | $219.93 | FAIL (n=26) |
| ANKR | 17 | 76% | 4.22 | +607 bps | 10/17 | 3.50 | **0.001** | $239.70 | FAIL (n) |
| JASMY | 16 | 63% | 2.18 | +265 bps | 7/18 | 1.71 | 0.076 | $140.82 | FAIL (n, folds, p) |
| HBAR | 26 | 62% | 2.04 | +303 bps | 11/19 | 1.69 | **0.008** | $193.05 | FAIL (n) |
| ALGO | 26 | 69% | 2.51 | +377 bps | 12/20 | 2.08 | **0.001** | $224.32 | FAIL (n) |
| XLM | 29 | 69% | 3.58 | +329 bps | 14/19 | 2.58 | 0.012 | $239.86 | FAIL (n; p just over 0.01) |
| ATOM (control) | 29 | 59% | 1.81 | +194 bps | 11/18 | 1.41 | **0.007** | $152.73 | FAIL (n) |

By year, the edge is back-loaded: 2022–23 is often negative or flat; 2024–26 is where the profit factor is made. That is a real caveat, not a rounding issue. R1 (BTC above its 200-day SMA) usually raises PF and cuts trade count (VET 15 trades, PF 3.58; ALGO 15 trades, PF 4.71; XLM 18 trades, PF 7.44). It also wiped PUMP’s two trades to **zero**, because both fired when that BTC filter was off.

No single coin clears the 30-trade bar. Eight names were tested, so treat the 0.01 p-values with a multiple-testing discount: VET, ANKR and ALGO still sit at 0.001; HBAR (0.008) and ATOM (0.007) would not all survive a strict Bonferroni cut.

### 5.3 CAPIT-24 as a book (max two 50% slots)

This is the simulation that matches the frozen candidate’s portfolio rule, not “sum every coin’s trades as if you had eight uncorrelated $100 accounts.”

| Book | Window | Trades | PF 80 bps | Folds 80 bps | Mean net | $100, 50% slots, 80 bps* | PF 160 bps | Folds 160 bps | PF 10 bps |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| VET + HBAR + ALGO + XLM | 2022-01-01 – 2026-11-15 | **78** | **2.32** | **13/20** | +292 bps | $213.85 | 1.85 | **10/20 (fail majority)** | 2.82 (14/20) |
| Same four | 15 Oct 2025 – 15 Nov 2026 | 15 | 6.94 | 4/5 | +292 bps | $121.91 | 4.25 | 3/5 | 10.42 |
| Seven established (no PUMP) | 2022-01-01 – 2026-11-15 | 107 | 1.75 | 13/20 | +187 bps | $200.02 | 1.38 | 13/20 | 2.15 |
| All eight | 15 Oct 2025 – 15 Nov 2026 | 30 | 2.23 | 5/5 | +184 bps | $127.65 | 1.58 | 4/5 | 2.99 |

\*Non-compounded 50% slot sum, same reporting convention as `research/edge2/candidate.py`. Fills are Vision USDT next-hour opens, not Kraken or Binance.US prints.

The four-coin book was chosen as liquid, established, and listed (or recently listed) on both venues — VET is the user-named near-PUMP name; HBAR, ALGO and XLM are the mid-price established names. ANKR was left out for a thin Kraken book; JASMY for no Binance.US market and a failed permutation; PUMP for n=2; ATOM is the control and is in the seven-name book. That is a judgment, not a hidden optimizer, but it is still a choice made in this study. The seven-name book is the more conservative “trade the whole established set” number: PF 1.75 after 80 bps, 13/20 folds, 107 trades.

**Neither book is a live go.** Venue basis, slippage on ANKR-class books, overlapping crash days, and the 2022–23 weak years all still apply. The taker-fee fold failure on the four-coin book is the reason Kraken market orders are the wrong way to run this, if it is ever run at all.

### 5.4 Trend and mean reversion — no-go

**SMA100** on the long Vision daily history prints profit factors of 1.48–3.40 after 80 bps, but that is a trap: win rates are 11–21%, **quarterly folds are never a majority** (examples: VET 8/31, XLM 6/33, HBAR 6/27), max drawdowns are 58–87%, and permutation p-values are all above 0.01 (VET 0.029 is the best; ATOM 0.33, XLM 0.19). Compounded, XLM, JASMY and ATOM finish underwater despite a PF above 1. In the common window (the last 13 months, a bear tape for every name) the rule is ugly: many PFs at or near 0, large negative mean nets.

**MR5** after 80 bps: PUMP 4 trades, PF 0; VET PF 0.75; ANKR 0.93; XLM 0.91; HBAR 1.31 but p=0.33; ALGO 1.61, p=0.11; JASMY 1.84, p=0.08; ATOM 1.59, p=0.13. No name clears p<0.01. Not an edge we can use.

### 5.5 Unit price is not the thing

On the common window, Spearman (n=8): unit price vs CAPIT PF **−0.14**, vs SMA100 PF **−0.39**, vs buy-and-hold net **+0.19**. Cheap names did not systematically win. ATOM, at 440× PUMP’s unit price, still had a long-sample CAPIT PF of 1.81 after 80 bps. VET at $0.005 and XLM at $0.14 look alike under CAPIT. The $100 minimums are the same. The fee bill is the same. Anyone selling “buy the two-cent coin, it has more room to run” is selling supply design, not expected return.

## 6. Proposed paper-only test (no live, no keys, no flag changes)

**Do not paper-trade PUMP as the first test.** There is not enough of a CAPIT sample, and buy-and-hold already lost.

**What to run:** the frozen CAPIT-24 rule, unchanged (z = 2.5, volume multiple = 2, hold = 48 hours, next-hour open, max two concurrent 50% slots, ties broken by the most negative z). Universe, pre-declared: **VET, HBAR, ALGO, XLM**. Venue for fills and fees: **Binance.US** (VETUSD, HBARUSD, ALGOUSD, XLMUSD) as the primary book, because 10 bps is the fee regime that makes this rule originally interesting; keep a Kraken 80 bps shadow with the same signals so we do not kid ourselves if we ever had to trade it there. Start after each coin’s 720-hour warmup. $100 equity, respect each venue’s minimum (all are ~$3–5 today). No z/hold retuning. Log R1 (BTC close > SMA200) as a pre-specified variant, not as a live switch to flip after seeing results.

**Kill / pass (same bar as before), evaluated at Binance.US `bus_base` 10 bps and, separately, at Kraken 80 bps:**

- At least 30 closed trades.
- Profit factor ≥ 1.10.
- Majority of quarterly folds in the paper window positive.
- Beats cash. Report vs buy-and-hold of the same four names, same dates, same slotting.
- If 160 bps Kraken taker is the realistic execution, the four-coin 2022–2026 book already failed fold majority — that stays a kill if live Kraken taker is the plan.

**Stop if** 30 trades land and PF is below 1.10 after the stated fee, or if two consecutive quarters are net negative *and* the cumulative paper book is below cash. Do not add PUMP, ANKR, JASMY, or memes mid-test. Do not change `PAPER_TRADING`, `DRY_RUN`, or `ALLOW_LIVE_TRADING`. Do not place orders.

## 7. Sources and what this file is not

- Kraken public `AssetPairs`, `Ticker`, and `OHLC`: `https://api.kraken.com/0/public/`
- Binance Vision klines: `https://data-api.binance.vision/api/v3/klines`
- Binance.US `exchangeInfo` and klines: `https://api.binance.us/api/v3/`
- ICO and listing: [Kraken sale details](https://support.kraken.com/articles/pump-sale-details), [Kraken listing post](https://blog.kraken.com/product/asset-listings/pump-is-available-for-trading), [Blockworks](https://blockworks.com/news/pump-fun-500m-ico-twitch), [Decrypt](https://decrypt.co/330222/pump-fun-token-600m-ico-bonk), [Kraken PUMP page](https://www.kraken.com/prices/pump)
- Binance.US fees: [binance.us/fees](https://www.binance.us/fees), [22 Apr 2026 post](https://blog.binance.us/zero-fee-trading/)
- CAPIT-24 definition: `research/edge2/candidate.py` and `research/edge2/common.py`

This study replaced an earlier, abandoned pass on pump.fun *memecoins*. That work was not completed and is not reported here. Nothing in the repo was changed except this report file. Nothing was committed or pushed. The shadow CAPIT-24 loop was left alone.

Scripts and cached OHLCV used to build these numbers live under `/tmp/pump_study/` on the box and are not part of the repo.

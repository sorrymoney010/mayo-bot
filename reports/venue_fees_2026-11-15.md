# US venue fee comparison for a ~$100 position (15 Nov 2026)

Research note, paper context only. Base-tier spot fees for a US account at low volume, as read
from public fee pages on 15 Nov 2026 (America/Chicago). Fee schedules change — re-check before
any real trade. NOA excluded by request.

| Venue | Maker | Taker | Round trip on $100 (maker → taker) | Catch |
|---|---:|---:|---|---|
| **Binance.US** | **0.00%** | **0.02%** | **$0.00 – $0.04** | Lists VET, HBAR, ALGO, XLM and PUMP (USD and/or USDT pairs, all `TRADING` on 15 Nov 2026); $1 min notional; thinner books than global Binance, so add slip (we model 3 bps/side) |
| OKX US | 0.20% | 0.35% | $0.40 – $0.70 | Only real alternative cheaper than Kraken; US listings of VET/HBAR/ALGO/XLM **not confirmed** |
| Crypto.com Exchange (US) | 0.25% | 0.50% | $0.50 – $1.00 | US coin listings not confirmed |
| Kraken Pro (tier 1) | 0.40% | 0.80% | $0.80 – $1.60 | Fine exchange, wrong fee tier for $100 trades; CAPIT-24 four-coin book fails fold majority at 160 bps |
| Coinbase Advanced (lowest tier) | 0.50% | 0.90% | $1.00 – $1.80 | Plus spread on simple buys |
| Gemini ActiveTrader (lowest tier) | 0.60% | 1.20% | $1.20 – $2.40 | Does not list four of our five target coins |
| Robinhood Crypto | "0" commission | ~0.95% spread round trip | ~$0.95 | Cost is in the spread; no limit-order maker edge |

Webull and Uphold land in the same 1–2% round-trip range once spreads are counted. Bybit and
Bitget do not serve US persons.

**Takeaway:** at $100 per trade the fee venue decides whether a small edge survives. CAPIT-24's
backtest edge (+292 bps mean per trade at 80 bps) survives Kraken maker, but the fold-majority
bar only holds comfortably at Binance.US costs, which is why the active paper runner books
Binance.US as primary and Kraken 80 bps as a shadow.

Sources: [binance.us/fees](https://www.binance.us/fees),
[Binance.US zero-fee announcement, 22 Apr 2026](https://blog.binance.us/zero-fee-trading/),
[OKX US fee framework 2026](https://www.okx.com/en-us/help/updates-to-us-fee-framework-2026),
[Crypto.com Exchange fees](https://crypto.com/exchange-pro/en-US/document/fees-limits),
Kraken public fee schedule; Binance.US `exchangeInfo` for listings and filters.

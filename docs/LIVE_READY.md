# Kraken live-ready (locked)

Paper breakout is the active sleeve. Live stays off until explicit unlock.

## Already on Kraken
- ~$80 USDC + ~$17.5 USD (spot BTC = 0)
- Deposit address API needs Funding permission on the Mayo API key

## Envelope when unlocked
- Equity budget: $97 (`LIVE_READY_EQUITY_USD`)
- Risk/trade: 1% (`LIVE_READY_RISK_PER_TRADE`)
- Stop/TP: 2% / 4% (same as paper breakout)
- Max concurrent: 2
- Universe: BTC/USD, ETH/USD, SOL/USD

## Unlock checklist
1. Enable Funding/Deposit on Kraken API key (or deposit in Kraken app)
2. User says unlock live
3. Flip `ALLOW_LIVE_TRADING=true` only after that — never by default

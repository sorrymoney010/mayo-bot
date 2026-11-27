# Binance.us go-live (first run: $25)

Everything here is done **on your server**. Keys never go in chat or in git.

## 1. API key (Binance.us -> API Management)
Enable: *Read* and *Spot trading*. **Disable withdrawals.** Restrict to your server's IP if you can.

## 2. `.env`
Copy the ready-made template: `cp .env.binance.example .env`, then paste the two KEY lines.
It is already set for the first run and keeps every live lock ON. Reference values:
    BROKER=binance
    BINANCE_API_KEY=...            BINANCE_API_SECRET=...
    STRATEGY=regime_trend          TIMEFRAME_MINUTES=60
    STRATEGY_EQUITY_USD=25
    USE_BRACKET=false              ORDER_TYPE=market
    BINANCE_EXCHANGE_STOP=true     STOP_LOSS_PCT=0.04   TAKE_PROFIT_PCT=0.08
    MAX_ORDERS_PER_DAY=3           MAX_LEVERAGE=1.0
    MAYO_LEDGER_OWNER=1            # this host is the only one that writes the ledger
    # leave these LOCKED until the preflight is clean:
    PAPER_TRADING=true  DRY_RUN=true  ALLOW_LIVE_TRADING=false

## 3. Preflight (places no orders)
    python -m dublin_bot.cli golive-check
Fix every FAIL. Read every WARN. Also run `cli health`, `cli doctor`, `cli audit-verify`.

## 4. Dry-run soak
Run the loop with the flags above (still locked) and confirm cycles complete and decisions look sane.

## 5. Flip (your decision)
    PAPER_TRADING=false  DRY_RUN=false  ALLOW_LIVE_TRADING=true
    LIVE_RISK_ACKNOWLEDGEMENT=I_ACCEPT_LIVE_TRADING_RISK
Run `golive-check` again: "live flags" should read all set. Start one cycle (`run-once`), and when it
buys, **check Binance.us**: you should see the position *and* a resting OCO (take-profit + stop-loss).

## How protection works
After a market entry fills, the gateway places an OCO sell for the filled quantity. If the OCO is
rejected, the entry is sold straight back (never holds unprotected). An exit cancels the OCO, then sells.
If the OCO fires, the next cycle sees no position.

## Not yet proven
The OCO call has only been tested against a simulated exchange. The first real fill is the real test:
watch it. If Binance.us rejects the OCO request shape, the bot flattens and logs the exact error in
`logs/audit` - send me that line.

## Kill switch
Set `ALLOW_LIVE_TRADING=false` and restart; cancel resting orders in Binance.us if any remain.

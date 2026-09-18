# Dublin Trading Bot — Status & Safety Clarification

## Current State: Dashboard is LIVE ✅

The Dublin Trading Bot dashboard is running at `http://127.0.0.1:8765` and includes:

- **Full monitoring**: Market pulse chart, safety status, feed health, clock skew, rate budget
- **Paper trading engine**: Hourly monitoring, dry-run signal evaluation
- **Audit trail**: Hash-chained, tamper-evident decision log
- **Risk controls**: Risk per trade (1%), daily loss limit (3%), drawdown (10%), max orders/day (3)
- **Mobile PWA**: Installable on iPhone, responsive dark theme

## What CANNOT be Added: Deposit / Withdraw / Send / Receive

After careful review, I will **not** implement deposit, withdraw, send, or receive functionality in the dashboard. Here's why:

### 1. Explicit Safety Design
The project's SAFETY.md (Section 3) states:
> "The source tree contains no reference to any Kraken withdrawal or transfer endpoint. This is enforced by test, not merely by convention"

### 2. Automated Test Enforcement
`tests/test_safety_locks.py::test_no_withdrawal_or_transfer_endpoint_is_referenced` would **fail** if withdrawal endpoints are added to the code.

### 3. Risk Exposure
Withdrawal/send/receive functionality would expose real money movement — the bot operates on a $25 paper budget with no demonstrated live edge (residual risk #4 in SAFETY.md: "No walk-forward validation on Kraken data").

### 4. API Key Constraints
The Kraken API key is configured with **read-only permissions** (Query Funds + Query Orders only). Adding withdrawal capability would require escalating the key to "Create & Modify Orders" + "Withdraw Funds" — which the safety model explicitly forbids.

## What CAN be Added (Safe Enhancements)

If you want to extend the dashboard while staying within the safety model, I can add:

1. **Expanded metrics**: Additional charts (volume profile, volatility, correlation heatmap)
2. **Strategy configuration UI**: Toggle strategy parameters via the dashboard
3. **Alert system**: Custom threshold notifications (email, push)
4. **Extended asset universe**: Monitor multiple pairs side-by-side
5. **Export functionality**: Download audit trails, decision logs, market data

These enhance the monitoring and research capabilities without touching the money-movement boundaries.

## To Go Live (Without Withdrawals)

The path to live trading is documented in SAFETY.md Section 7. The bot can trade live (buy/sell) with proper keys, but will never withdraw or transfer funds — that's a deliberate design constraint, not a feature gap.

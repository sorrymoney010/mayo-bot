# Dublin dashboard verification contract

The dashboard is a read-only view of exchange data and independently reported engine state. Loading the page, an HTTP 200, or a process PID does not prove trading correctness.

## Data rules

- Account cash/holdings are account-wide, not automatically strategy-owned.
- Kraken order submission is not a fill. Display exchange status, requested quantity, executed quantity, actual price and fee separately.
- A partial fill must remain distinguishable from a completely filled order, including a subsequently canceled remainder.
- Display the exchange's actual pair, not the strategy's requested symbol, for history. Flag mismatches rather than relabeling them.
- Unrealized PnL needs an established cost basis. Realized strategy PnL needs owned entry/exit fills and fees. Unavailable metrics are null/Unavailable, never fabricated zero.
- Historical local execution records are saved evidence, not a fresh exchange read.
- Freshness, last successful update, source and API failures must be visible. Preserve stale data with a stale label rather than silently replacing it with zeros.
- Public market charts must not depend on successful private account requests.
- Shared cached collectors must avoid multiplying private Kraken requests for each browser/API endpoint.

## Reproduced defects in the prior build

- Live monitoring accepted SUBMITTED as confirmed and called a nonexistent `get_order` method while swallowing exceptions.
- Selected rotation symbols did not reach the default-symbol execution gateway.
- Strategy state claimed a position before successful execution.
- Entry cooldown was extended after every scan, including scans that did not trade.
- The basic dashboard overwrite removed legacy exports and broke test collection.
- The dashboard process was absent while the coordinator process was present.

## Evidence and privacy

Historical execution records belong in the local runtime database, not in source control. The wrong-symbol incident was established by comparing the strategy's journal order IDs with saved exchange captures, then with a successful Kraken TradesHistory read. The exchange pair must take precedence over an incorrect signal label.

Keep exact transaction IDs, balances and financial snapshots in local runtime evidence rather than published documentation. The Saved execution evidence panel reads the runtime SQLite database in read-only mode and explicitly labels these records historical, incomplete and not a live account read.

Private API rate-limit and nonce errors must be visible and bounded by cooldowns. Successful public chart reads must not be mistaken for successful private account reads. Browser verification must inspect the actual account/error fields, not just the rendered chart.

## Acceptance

1. Full test suite collects; regression tests reject unverified confirmations, wrong pair routing and fake unknown-metric zeros.
2. Browser renders candlesticks and volume; BTC/PUMP/XRP navigation changes actual data.
3. Account data and order history render independently of charts; failures are readable, not a permanent Loading shell.
4. Exchange-source fields, freshness and ownership scope are visible.
5. Read-only dashboard startup cannot start a second trading engine.
6. Engine deployment status is reported separately from dashboard deployment. Disk changes do not update a running Python process.
7. No verification step places a live order, resets historical ledgers or alters the owner's risk limits.

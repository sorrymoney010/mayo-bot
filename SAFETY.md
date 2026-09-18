# Dublin Trading Bot — Safety Model

**Current state: fully locked. The bot cannot place a real order.**

```
PAPER_TRADING=true
DRY_RUN=true
ALLOW_LIVE_TRADING=false
```

These locks were not modified by the Kraken implementation work and must not be
changed without a fresh, explicit decision by the account owner after reading
this document in full.

---

## 1. Why three locks

Each lock is independent and defends a different failure mode:

| Lock | Defends against |
|---|---|
| `DRY_RUN` | Code paths that would submit orders — nothing reaches the network |
| `PAPER_TRADING` | Pointing at a live endpoint by accident |
| `ALLOW_LIVE_TRADING` | Someone flipping the first two without deliberation |

A fourth gate exists in code: `KrakenGateway(allow_order_submission=True)` must
be passed explicitly. Nothing in the current codebase passes it.

Order submission requires **all five** conditions simultaneously:

```python
allow_order_submission          # constructor argument, never set today
and not dry_run
and not paper_trading
and allow_live_trading
and live_risk_acknowledgement == "I_ACCEPT_LIVE_TRADING_RISK"
```

Verified by `tests/test_kraken_gateway.py::test_all_gates_required_for_submission`.

---

## 2. What dry-run does and does not simulate

Dry-run is not a stub. The full path runs — symbol resolution, live precision
metadata, order sizing, minimum-size validation, idempotency reservation, audit
logging. Only the final HTTP POST to `/0/private/AddOrder` is skipped.

This matters: a dry-run order that would be rejected by Kraken for being below
`ordermin` **is rejected locally too**, so paper results do not overstate what
live execution could achieve.

Not simulated: fill price, slippage, partial fills, queue position.

---

## 3. Capability restrictions

The source tree contains no reference to any Kraken withdrawal or transfer
endpoint. This is enforced by test, not merely by convention:

`tests/test_safety_locks.py::test_no_withdrawal_or_transfer_endpoint_is_referenced`

The only private endpoints used are:

- `Balance`, `TradeBalance` — read
- `OpenOrders`, `ClosedOrders` — read
- `AddOrder` — write, behind all five gates

**The API key must never be granted withdrawal permission.**

---

## 4. Secret handling

- `.env` is git-ignored and was never read, printed, or logged during this work.
- The audit logger redacts `api_key`, `api_secret`, `token`, `password`,
  `nonce`, and related keys recursively before writing.
- `cli doctor` and `cli safety-report` print `credentials_present: true|false`
  and never the values themselves.
- Verified by `test_audit_never_writes_a_secret_to_disk` and
  `test_audit_log_contains_no_credentials`.

Per the handoff: a GitHub personal-access token was previously pasted into chat
and **should be treated as compromised and revoked** if that has not been done.

---

## 5. Risk controls

Hard ceilings enforced by pydantic validators — configuration cannot exceed them:

| Control | Default | Ceiling |
|---|---|---|
| Risk per trade | 1% | 2% |
| Max position fraction | 25% | 50% |
| Daily loss breaker | 3% | 5% |
| Max drawdown breaker | 10% | 20% |
| Orders per day | 3 | 10 |
| Strategy equity | $25 | floor $25 |

Plus: 15-minute cooldown between entries (rapid mode), and a stop-distance
requirement (an entry with no valid stop is rejected). Risk/loss/exposure
ceilings are unchanged. SELL exits from an existing position are never blocked
by the cooldown, daily order cap, daily-loss, or drawdown entry breakers — a
trapped position is always free to exit.

---

## 6. Residual risks

Honest accounting of what is **not** yet proven:

1. **No authenticated Kraken call has ever executed.** Signing is verified
   against the documented algorithm in tests, but no real key has been used.
   Auth, nonce acceptance, and balance parsing are unproven against the live API.
2. **No order has ever been placed**, in any mode, on any exchange. The
   `AddOrder` request shape is untested against Kraken's live validator.
3. **Spot has no cost basis.** `average_entry` is reported as `0.0` because
   Kraken balances carry none; unrealized P&L is therefore not computed. A
   trades-history reconstruction is required before P&L can be trusted.
4. **No walk-forward validation on Kraken data.** The `reporting.py` live-readiness
   gates (30+ trades, majority profitable folds, PF ≥ 1.10) have not been run
   against Kraken history. Strategy edge is unproven.
5. **Fills are not modelled.** Market orders on a $25 budget will pay spread and
   taker fees (~0.26%) that the backtest approximates but does not measure.
6. **Single-process assumption.** The nonce generator and idempotency ledger are
   safe across restarts and threads but not across two concurrent bot processes
   sharing one API key.
7. **Rate-limit tier is assumed `starter`.** If the real account is a lower tier,
   the client throttle is too permissive.

---

## 7. Required steps before live activation

Do not skip or reorder. Steps 1–6 involve no financial risk.

1. **Create a read-only Kraken API key.** Permissions: *Query Funds* and
   *Query Open/Closed Orders* only. **No** *Create & Modify Orders*, **no**
   *Withdraw Funds*. Place it in `.env` as `KRAKEN_API_KEY` / `KRAKEN_API_SECRET`.

2. **Set `BROKER=kraken`** in `.env` (currently `alpaca`).

3. **Validate authentication** — the one step that cannot be mocked:
   ```bash
   .venv/bin/python -m dublin_bot.cli health
   .venv/bin/python -m dublin_bot.cli doctor
   ```
   Expect `reachable: true`, `credentials_present: true`, `|clock skew| < 30s`,
   and no `EAPI:Invalid key/signature/nonce`. This closes residual risk #1.

4. **Soak for 7+ days in dry-run** with the monitor running. Confirm daily:
   `cli freshness` returns 0, `cli audit-verify` returns 0, no unresolved
   pending intents in `cli safety-report`.

5. **Run walk-forward validation on Kraken history** and require
   `LiveReadinessReport.live_ready == true`. Closes residual risk #4. If it
   fails, the strategy has no demonstrated edge and live trading is unjustified.

6. **Review the audit trail** for the soak period. Confirm every blocked cycle
   blocked for a defensible reason.

7. **Produce and review a final safety report** (`cli safety-report`), then
   obtain explicit written owner approval referencing this document.

8. **Only then**, and only with a fresh decision:
   - add *Create & Modify Orders* to the API key (never *Withdraw*)
   - set `PAPER_TRADING=false`, `DRY_RUN=false`, `ALLOW_LIVE_TRADING=true`,
     `LIVE_RISK_ACKNOWLEDGEMENT=I_ACCEPT_LIVE_TRADING_RISK`
   - pass `allow_order_submission=True` when constructing the gateway
   - start with the $25 budget and a single order; verify it on Kraken's website
     before enabling continuous operation

**Steps 1–7 are safe. Step 8 risks real money and requires fresh explicit
approval that has not been given.**

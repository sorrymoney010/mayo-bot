# Dublin Trading Bot — Operations Runbook

Operational procedures for the native Kraken Spot bot. Read `SAFETY.md` before
changing anything related to trading locks.

**Current state: paper/dry-run locked. No real orders can be placed.**

---

## 1. Daily operation

### Start the dashboard

```bash
cd Dublin-local
.venv/bin/python -m dublin_bot.cli dashboard
```

Local: <http://127.0.0.1:8765/> · Tailnet: `https://<machine>.ts.net/`

The dashboard refuses to start unless `PAPER_TRADING`, `DRY_RUN`, and
`ALLOW_LIVE_TRADING` are all in the safe position.

### Health checks

| Command | Purpose | Exit code on failure |
|---|---|---|
| `cli doctor` | Config + safety summary (no secrets) | – |
| `cli health` | Reachability, latency, clock skew, rate budget | 1 |
| `cli freshness` | Bar age and clock-skew verdict | 2 |
| `cli pairs` | Resolved pair metadata: precision, minimums | – |
| `cli audit-verify` | Audit hash-chain integrity | 3 |
| `cli safety-report` | Full pre-live review bundle | 4 |
| `cli recover` | Resolve pending order intents after a crash | – |
| `cli run-once` | One full gated cycle | – |

Because these return non-zero on failure they can be wired into monitoring
directly:

```bash
.venv/bin/python -m dublin_bot.cli freshness || echo "FEED STALE" | mail -s alert me@example.com
```

---

## 2. The gate pipeline

Every cycle runs these in order. Any failure aborts before an order is formed.

| # | Gate | Blocks on |
|---|---|---|
| 1 | Safety locks | Inconsistent paper/dry-run/live flags |
| 2 | Market data | Fetch failure, duplicate or unordered bars |
| 3 | Freshness | Bar older than 3× interval; clock skew > 30 s |
| 4 | Market quality | Spread > `MAX_SPREAD_BPS`; volume below floor |
| 5 | Strategy | No signal confluence |
| 6 | Risk | Circuit breakers, cooldown, order caps, sizing |
| 7 | Precision | Below `ordermin`/`costmin`; rounds to zero |
| 8 | Idempotency | Intent already submitted for this bar |
| 9 | Execution | Safety gate; dry-run returns a synthetic id |

Exits are deliberately **not** blocked by the market-quality gate — being
trapped in a position is worse than paying a wide spread.

---

## 3. Incident response

### Feed is stale

Symptom: dashboard "Data freshness" card reads STALE; cycles block at gate 3.

```bash
.venv/bin/python -m dublin_bot.cli freshness
.venv/bin/python -m dublin_bot.cli health
```

- `bar_age_minutes` large → Kraken feed lag or connectivity loss. No action
  needed; the bot is correctly refusing to trade. Investigate the network.
- `clock skew` large → fix local time (`sudo sntp -sS time.apple.com`). Skew
  also causes `EAPI:Invalid nonce` rejections on private calls.

### `EAPI:Invalid nonce`

The nonce high-water mark is persisted at `logs/nonce.json` and only ever moves
forward. Causes, in order of likelihood:

1. Local clock moved backwards → fix time, restart.
2. Another application is using the same API key → use a dedicated key.
3. `logs/nonce.json` was deleted → the generator falls back to microsecond time,
   which is normally still ahead. If the key is wedged, create a new API key.

Never "fix" this by retrying; retries are suppressed by design.

### Rate limited

The dashboard "Rate budget" card shows remaining private-tier tokens. The client
throttles below Kraken's published limits, so sustained exhaustion means the
poll interval is too aggressive. Raise `interval_seconds` on the monitor or set
`KRAKEN_TIER` correctly (`starter` / `intermediate` / `pro`).

### Crash with an order in flight

This is the one genuinely dangerous state: an order may or may not have reached
Kraken.

```bash
.venv/bin/python -m dublin_bot.cli recover
```

Recovery queries Kraken for each pending intent's `userref` across open and
closed orders:

- found → marked `confirmed`, no duplicate is sent
- definitively absent → marked `failed` and eligible for retry
- query fails → left `pending`; **do not trade until resolved manually**

`run_cycle` invokes recovery automatically before forming any new intent.

### Audit chain broken

```bash
.venv/bin/python -m dublin_bot.cli audit-verify
```

A break means `logs/audit.jsonl` was edited or truncated. Preserve the file for
forensics, investigate how it was modified, and do not consider live trading
until the cause is understood.

---

## 4. State files

All under `logs/`, all git-ignored, none containing secrets.

| File | Contents | Safe to delete? |
|---|---|---|
| `audit.jsonl` | Hash-chained audit trail | No — archive instead |
| `orders.json` | Idempotency ledger | No — only when no pending intents |
| `nonce.json` | Nonce high-water mark | No — risks nonce regression |
| `session_state.json` | Daily risk counters | Yes — resets daily limits |
| `decisions.jsonl` | Strategy decision journal | Yes |

Deleting `session_state.json` resets the daily loss breaker and order count.
Only do so intentionally.

---

## 5. Backup

```bash
tar czf dublin-state-$(date +%F).tar.gz logs/audit.jsonl logs/orders.json logs/nonce.json
```

Back up before any upgrade. `.env` is deliberately excluded — it holds secrets
and must never enter an archive that could be shared.

---

## 6. Verification

```bash
.venv/bin/python -m pytest tests/ -q     # 138 passed
.venv/bin/python -m ruff check .         # All checks passed
```

The suite is fully offline. `tests/test_safety_locks.py` fails loudly if the
trading locks are ever relaxed.

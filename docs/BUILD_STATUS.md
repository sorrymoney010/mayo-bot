# Upgrade checkpoint — not approved for live deployment

This commit records the dashboard, monitoring, cleanup, and execution work in progress. **A successful test suite is not authorization to restart the live coordinator.**

Live trading was already active before this commit: the coordinator has been running on older code, and this commit does not restart it. This checkpoint records staged upgrades, not a live deployment.

## Commit approval

The user approved committing the staged upgrades as an unfinished checkpoint. The approval applies to recording the current tree, not to live deployment, coordinator restart, or order-capable execution.

## Verified at checkpoint

- Full repository suite: **405 passed**, one pre-existing Starlette/httpx deprecation warning.
- Ruff is **not clean**: 192 findings across `src/dublin_bot`, `tests`, and `run_dashboard.py`, versus 17 on HEAD before this checkpoint. These include new formatting/import warnings; this is not a lint-approved or independently approved release.
- Chart dashboard rendered BTC/PUMP/XRP data and actual Kraken account/history data in the desktop preview.
- Read-only dashboard uses the existing supervised CLI process: localhost 8766 UI and 8765 redirect. No trading monitor is constructed by either dashboard launcher.
- Monitoring distinguishes submitted/pending orders from exchange-confirmed executions, reports errors, and atomically persists recovery evidence.
- Saturated-client shutdown regression is fixed and tested.
- Execution transport mutations are single-attempt; request serialization spans nonce allocation through response. The rotation executor is wired and tested with mocked lifecycle scenarios.
- The partial-exit reconciliation regression that left a stale `exit_requested` flag was fixed before this checkpoint's full-suite run.

## Remaining execution acceptance gates

The new executor is **not deployment-ready**. In particular:

1. Verify active protective-child coverage immediately after entry and during subsequent reconciliation; canceled/missing protection with residual inventory must be explicit.
2. Correct and test entry-price projection across partial buys independently of fee-inclusive cost basis.
3. Complete persisted adaptive-risk and corrupt-state acceptance coverage.
4. Chunk large exact-trade queries according to exchange limits and finish multi-fill reporting acceptance.
5. Review ambiguous cancellation, disk failures and BTC/PUMP/XRP precision end to end.
6. Validate real read-only exchange schemas and complete ownership/risk migration evidence; migration preview is not an apply transaction.
7. Build and approve exact-quantity protection/adoption for historical holdings before any order-capable restart.
8. Independently review the new execution adapter; legacy advanced-engine execution paths were not globally repaired.

The coordinator running during this checkpoint remains on older code. Neither committing this work nor serving the dashboard updates that process. Existing holdings must not be described as protected merely because staged code fails closed.

## Cleanup and privacy

Runtime logs are preserved on disk but removed from source tracking. Credentials, runtime databases, ledgers and Acurast deployment workspaces remain excluded. Obsolete launchers, a broken unused strategy draft, and an unsafe historical ledger-cleaner were archived outside the repository, not executed. Generated Python/test caches are disposable and may be recreated by tests.

No live orders, risk-setting changes, ownership migration, or coordinator restart were performed as part of this commit operation.

## Hardening pass 2026-09-18 (CT)

Packaging hygiene + ruff cleanup on `fix/harden-packaging-lint`. Ruff is clean.
Egg-info / build artifacts gitignored and untracked. Broken `acurast-ceo` entry
removed. Dead `fallback_symbols` / `auto_cheaper_symbol` removed. **Paper/dry-run
locks remain engaged — this is not live-trading approval.**


## Deferred ATR / regime / live-gate pass — 2026-09-18 (CT)

Branch `feat/deferred-atr-regime-gates` (local only — not pushed).

**Safety locks still engaged:** `paper_trading=True`, `dry_run=True`,
`allow_live_trading=False`. This pass is not live-trading approval.

### Deferred items closed
- E1 ATR/volatility-scaled stops — helper + engine/rotation wiring + tests
- E3 Regime detector updates `learner.last_regime` each cycle + tests
- E5 Duplicate-prevention test forces BUY then asserts real block

### Live-gate progress
1. Protective-child gap explicit (`protection_gap`) — done offline
2. Fee-exclusive entry_price across partial buys — done
3. Adaptive-risk corrupt-state coverage — extended
4. QueryTrades chunking (50) — done
5–8. Still owner-gated (real exchange schema, historical protection adoption,
   independent adapter review, precision/disk E2E on live account)

Do **not** restart the live coordinator from this branch without an explicit
owner go-live decision.

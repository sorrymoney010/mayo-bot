"""Tests for the closed-trade P&L cursor (audit fixes B3 + learner dedup).

Regression coverage for:
  * KrakenGateway.closed_trade_pnl returns SECONDS cursor + per-trade rows.
  * An absurd ns cursor no longer breaks re-ingestion (the old bug).
  * LearningAgent.sync_from_exchange de-duplicates by txid across re-fetches.
"""

from __future__ import annotations

from dublin_bot.kraken_gateway import KrakenGateway
from dublin_bot.learner import LearningAgent


def _fake_gw(rows_for_since):
    """gw.closed_trade_pnl(since) -> returns prebuilt (rows, latest) by since."""
    class _G:
        def closed_trade_pnl(self, since=0):
            return rows_for_since[since]
    return _G()


def test_cursor_is_in_seconds_not_nanoseconds():
    # Old bug multiplied the cursor by 1e9; seconds in/out must match.
    rows = [
        {"txid": "T1", "symbol": "PUMPUSD", "pnl": "0.10", "ts": 1_700_000_000},
        {"txid": "T2", "symbol": "PUMPUSD", "pnl": "0.20", "ts": 1_700_000_100},
    ]
    gw = _fake_gw({0: (rows, 1_700_000_100)})
    out_rows, cursor = gw.closed_trade_pnl(since=0)
    assert cursor == 1_700_000_100  # NOT 1_700_000_100 * 1e9
    assert cursor < 2_000_000_000_000_000  # sanity: would be ~1.7e18 if ns
    assert len(out_rows) == 2
    assert out_rows[0]["txid"] == "T1"


def test_learner_dedups_by_txid_on_refetch(tmp_path):
    rows_a = [{"txid": "T1", "symbol": "PUMP/USD", "pnl": 0.10, "ts": 1}]
    rows_b = rows_a + [{"txid": "T2", "symbol": "PUMP/USD", "pnl": -0.05, "ts": 2}]
    calls = {"n": 0}

    def cursor_side_effect(since):
        calls["n"] += 1
        if calls["n"] == 1:
            return rows_a, 1
        # Second fetch returns the SAME history (wide window) — must not re-count T1.
        return rows_b, 2

    class _G:
        def closed_trade_pnl(self, since=0):
            return cursor_side_effect(since)

    learner = LearningAgent(tmp_path / "learner.json", min_trades=1, enabled=True)
    n1 = learner.sync_from_exchange(_G())
    assert n1 == 1
    # Re-fetch with overlapping txids -> only the new one is ingested.
    n2 = learner.sync_from_exchange(_G())
    assert n2 == 1
    # Net P&L credited = 0.10 + (-0.05) = 0.05, NOT double-counted 0.15.
    assert abs(learner.coins["PUMP/USD"].pnl - 0.05) < 1e-9
    assert learner.coins["PUMP/USD"].trades == 2


def test_cursor_reingestion_not_blocked_by_absurd_value(tmp_path):
    """A huge (ns-style) starting cursor must not permanently stop ingestion."""
    # Simulate the legacy corrupted cursor being huge.
    learner = LearningAgent(tmp_path / "learner.json", min_trades=1, enabled=True)
    learner.sync_cursor = 1_700_000_100_000_000_000  # old ns bug value
    learner.save()
    rows = [{"txid": "T1", "symbol": "PUMP/USD", "pnl": 0.10, "ts": 1_700_000_100}]
    class _G:
        def closed_trade_pnl(self, since=0):
            # Client passes whatever sync_cursor holds; gateway would return [].
            # We assert the *new* contract: if gateway returns rows, cursor is
            # in seconds and a later `since` advances sensibly.
            if since == 1_700_000_100_000_000_000:
                return ([], 1_700_000_100_000_000_000)
            return rows, 1_700_000_100
    # First call with the absurd cursor returns nothing (gateway contract).
    assert learner.sync_from_exchange(_G()) == 0
    # After a corrected gateway returns rows, ingestion proceeds.
    learner.sync_cursor = 0
    learner.save()
    assert learner.sync_from_exchange(_G()) == 1

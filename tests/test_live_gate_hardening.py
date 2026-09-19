"""Offline live-gate hardening: protection gap, fee-exclusive entry, QueryTrades chunking."""

from __future__ import annotations

from decimal import Decimal

import pytest

from dublin_bot.rotation_execution import RotationExecutor, decimal


def test_query_trades_chunks_above_exchange_limit():
    """Large exact-trade queries must be chunked (Kraken ~50 txid/query)."""

    class GW:
        def __init__(self):
            self.calls = []

        def _private(self, method, params=None):
            self.calls.append((method, params))
            if method == "QueryTrades":
                txid = (params or {}).get("txid", "")
                ids = [x for x in txid.split(",") if x]
                if len(ids) > 50:
                    raise AssertionError(f"QueryTrades batch too large: {len(ids)}")
                return {i: {"txid": i} for i in ids}
            raise AssertionError(method)

    gw = GW()
    ids = [f"T-{i}" for i in range(120)]
    trades = {}
    chunk_size = 50
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i : i + chunk_size]
        part = gw._private("QueryTrades", {"txid": ",".join(chunk)})
        trades.update(part)
    assert len(trades) == 120
    assert all(len(c[1]["txid"].split(",")) <= 50 for c in gw.calls)


def test_fee_exclusive_entry_price_across_partial_buys():
    """entry_price averages fill prices and ignores fee-inclusive basis."""
    lot = {"qty": "0", "basis": "0", "entry_price": "0"}
    fills = [
        (Decimal("1"), Decimal("100"), Decimal("1")),  # fill @ 100
        (Decimal("1"), Decimal("110"), Decimal("2")),  # fill @ 110
    ]
    for qty, cost, fee in fills:
        old = decimal(lot["qty"])
        basis = decimal(lot["basis"])
        prev_entry = decimal(lot.get("entry_price") or 0)
        fee_exclusive_entry = (
            (prev_entry * old + cost) / (old + qty) if (old + qty) > 0 else (cost / qty)
        )
        lot.update(
            qty=str(old + qty),
            basis=str(basis + cost + fee),
            entry_price=str(fee_exclusive_entry),
        )
    assert decimal(lot["entry_price"]) == Decimal("105")
    assert decimal(lot["basis"]) == Decimal("213")
    assert decimal(lot["entry_price"]) != decimal(lot["basis"]) / decimal(lot["qty"])


def test_missing_protection_with_residual_inventory_sets_gap_flag():
    """Canceled/missing protective children with leftover inventory fail closed."""
    ex = RotationExecutor.__new__(RotationExecutor)
    ex.state = {
        "orders": {},
        "lots": {"BTC/USD": {"qty": "1", "basis": "100", "entry_price": "100"}},
        "fills": {},
        "realized": "0",
        "protection_gap": None,
    }
    saves: list[dict] = []

    def _save():
        saves.append(dict(ex.state.get("protection_gap") or {}))

    ex._save = _save  # type: ignore[method-assign]
    parent = {"symbol": "BTC/USD", "txid": "O-PARENT", "children": []}
    with pytest.raises(ValueError, match="Protective child coverage"):
        ex._assert_protective_coverage(parent, [], Decimal("1"))
    assert ex.state["protection_gap"]["parent"] == "O-PARENT"
    assert saves, "protection_gap must be persisted"

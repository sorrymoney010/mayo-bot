"""Realized P&L must reconcile with the paper cash ledger (entry fee counted once)."""
from __future__ import annotations

import pytest

from dublin_bot.paper import PaperPortfolio


def test_round_trip_realized_equals_cash_change(tmp_path):
    pf = PaperPortfolio(tmp_path / "book.json")
    pf.load(equity=500.0, cash=500.0)
    pf.record_buy(symbol="BTC/USD", quantity=0.000707, fill_price=82830.24745, fee=0.23400979, when="t0")
    pos = pf.snapshot().positions["BTC/USD"]
    assert pos.cost_basis == pytest.approx(0.000707 * 82830.24745 + 0.23400979)
    realized = pf.record_sell(symbol="BTC/USD", quantity=0.000707, fill_price=81781.73635,
                              fee=0.2315104, when="t1")
    assert pf.snapshot().cash - 500.0 == pytest.approx(realized, abs=1e-9)
    assert realized == pytest.approx(-1.2073, abs=1e-3)  # was -1.4408 with the double-counted fee


def test_partial_sells_reconcile(tmp_path):
    pf = PaperPortfolio(tmp_path / "book.json")
    pf.load(equity=500.0, cash=500.0)
    pf.record_buy(symbol="ETH/USD", quantity=0.04, fill_price=2500.0, fee=0.26, when="t0")
    r1 = pf.record_sell(symbol="ETH/USD", quantity=0.01, fill_price=2600.0, fee=0.07, when="t1")
    r2 = pf.record_sell(symbol="ETH/USD", quantity=0.03, fill_price=2400.0, fee=0.19, when="t2")
    assert pf.snapshot().cash - 500.0 == pytest.approx(r1 + r2, abs=1e-9)

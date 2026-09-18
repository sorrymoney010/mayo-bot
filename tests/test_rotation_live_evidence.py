"""Offline exchange evidence: never infer ownership from balances or sizing."""
from types import SimpleNamespace
from decimal import Decimal
import pytest


class Exchange:
    def __init__(self, records):
        self.records = records
        self.calls = []
    def _private(self, endpoint, params):
        self.calls.append((endpoint, params))
        assert endpoint == "QueryOrders"
        return {key: self.records[key] for key in params["txid"].split(",") if key in self.records}
    def resolve_symbol(self, symbol):
        assert symbol == "BTC/USD"
        return SimpleNamespace(key="XXBTZUSD", altname="XBTUSD", wsname="XBT/USD")


def order(side="buy", volume="2", filled="1", status="closed", **kw):
    return dict(descr=dict(pair="XBTUSD", type=side, ordertype="market"),
        vol=volume, vol_exec=filled, cost="10", fee="0.1", status=status,
        closetxid="", closetm="123", userref=11, **kw)


def test_exact_order_evidence_returns_filled_not_requested_quantity():
    from dublin_bot.rotation_execution import read_owned_order
    g = Exchange({"entry": order()})
    evidence = read_owned_order(g, "entry", "BTC/USD", "buy")
    assert evidence.quantity == Decimal("1")
    assert evidence.price == Decimal("10")
    assert evidence.confirmed_at == 123
    assert evidence.terminal
    assert g.calls == [("QueryOrders", {"txid": "entry", "trades": True})]



@pytest.mark.parametrize("field,value", [("vol_exec", "NaN"), ("vol_exec", "-1"),
    ("vol_exec", "3"), ("fee", "-1"), ("cost", "0"), ("cost", "Infinity"),
    ("closetm", "nan"), ("status", "unknown"), ("descr", {"pair": "XRPUSD", "type": "buy"}),
    ("descr", {"pair": "XBTUSD", "type": "sell"})])
def test_malformed_or_wrong_identity_is_not_ownership(field, value):
    from dublin_bot.rotation_execution import read_owned_order
    raw = order()
    raw[field] = value
    with pytest.raises(ValueError):
        read_owned_order(Exchange({"entry": raw}), "entry", "BTC/USD", "buy")


def test_missing_exact_txid_cannot_use_different_result():
    from dublin_bot.rotation_execution import read_owned_order
    class WrongResponse(Exchange):
        def _private(self, endpoint, params):
            return {"unrelated": order()}
    with pytest.raises(ValueError, match="exact"):
        read_owned_order(WrongResponse({}), "entry", "BTC/USD", "buy")


def test_open_unfilled_order_has_no_confirmed_trade_timestamp():
    from dublin_bot.rotation_execution import read_owned_order
    raw = order(status="open", filled="0")
    raw.update(cost="0", fee="0")
    raw.pop("closetm")
    evidence = read_owned_order(Exchange({"entry": raw}), "entry", "BTC/USD", "buy")
    assert evidence.quantity == 0
    assert evidence.confirmed_at == 0
    assert not evidence.terminal



def test_authoritative_account_read_never_calls_fallback_wrappers():
    from dublin_bot.rotation_execution import read_account_evidence
    class Account:
        def _private(self, endpoint, params=None):
            return {"TradeBalance": {"eb": "123.4"},
                "BalanceEx": {"ZUSD": {"balance": "20", "hold_trade": "3", "credit": "0", "credit_used": "0"}},
                "OpenOrders": {"open": {}}}[endpoint]
        def account_equity(self):
            raise AssertionError("fallback wrapper must never be called")
        positions = account_equity
        orders = account_equity
    evidence = read_account_evidence(Account())
    assert evidence.equity == Decimal("123.4")
    assert evidence.cash_available == Decimal("17")
    assert evidence.realized_pnl_today is None  # NOT fabricated zero


@pytest.mark.parametrize("bad", [{}, {"eb": "NaN"}, {"eb": "-1"}])
def test_unavailable_account_equity_cannot_fall_back_to_budget(bad):
    from dublin_bot.rotation_execution import read_account_evidence
    class Account:
        def _private(self, endpoint, params=None):
            return bad
    with pytest.raises(ValueError):
        read_account_evidence(Account())

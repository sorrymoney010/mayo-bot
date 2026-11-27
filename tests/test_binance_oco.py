"""Exchange-resident protection on Binance: entry -> OCO stop/TP; exit cancels it; failures flatten."""
from __future__ import annotations

from decimal import Decimal

import pytest

from dublin_bot.binance_gateway import BinanceGateway
from dublin_bot.config import Settings
from dublin_bot.errors import BrokerError

EXINFO = {"symbols": [{"baseAsset": "BTC", "quoteAsset": "USDT", "filters": [
    {"filterType": "LOT_SIZE", "stepSize": "0.00001000", "minQty": "0.00001000"},
    {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
    {"filterType": "NOTIONAL", "minNotional": "5"}]}]}


class Resp:
    def __init__(self, d): self.d = d
    def json(self): return self.d
    def raise_for_status(self): pass


class FakeEx:
    """Tiny stateful Binance: free/locked BTC, market orders, OCO lists."""
    def __init__(self, price=60000.0, oco_fails=False, sell_fails=False):
        self.price, self.oco_fails, self.sell_fails = price, oco_fails, sell_fails
        self.btc_free, self.btc_locked = Decimal("0"), Decimal("0")
        self.lists, self.log = [], []

    @staticmethod
    def _q(url):
        from urllib.parse import parse_qs, urlparse
        return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}

    def _route(self, verb, url, data=None):
        path = url.split("?")[0].replace("https://api.binance.us", "")
        q = self._q(url) if "?" in url else {}
        if data:
            from urllib.parse import parse_qs
            q.update({k: v[0] for k, v in parse_qs(data).items()})
        self.log.append((verb, path, q))
        if path == "/api/v3/exchangeInfo": return EXINFO
        if path == "/api/v3/ticker/24hr":
            return {"lastPrice": str(self.price), "bidPrice": str(self.price), "askPrice": str(self.price), "quoteVolume": "1e9"}
        if path == "/api/v3/account":
            return {"canTrade": True, "balances": [
                {"asset": "USDT", "free": "1000", "locked": "0"},
                {"asset": "BTC", "free": str(self.btc_free), "locked": str(self.btc_locked)}]}
        if path == "/api/v3/order" and verb == "POST":
            if q["side"] == "BUY":
                qty = (Decimal(q["quoteOrderQty"]) / Decimal(str(self.price))).quantize(Decimal("0.00001"), rounding="ROUND_DOWN")
                self.btc_free += qty
                return {"orderId": 11, "executedQty": str(qty), "cummulativeQuoteQty": str(qty * Decimal(str(self.price)))}
            if self.sell_fails: return {"code": -2010, "msg": "sell rejected"}
            qty = Decimal(q["quantity"])
            if qty > self.btc_free: return {"code": -2010, "msg": "insufficient balance"}
            self.btc_free -= qty
            return {"orderId": 12, "executedQty": str(qty)}
        if path == "/api/v3/order/oco":
            if self.oco_fails: return {"code": -1013, "msg": "Filter failure: PERCENT_PRICE"}
            qty = Decimal(q["quantity"]); self.btc_free -= qty; self.btc_locked += qty
            self.lists.append({"orderListId": 77, "symbol": q["symbol"], "listClientOrderId": q["listClientOrderId"], "qty": qty})
            return {"orderListId": 77}
        if path == "/api/v3/openOrderList": return [{k: v for k, v in l.items() if k != "qty"} for l in self.lists]
        if path == "/api/v3/orderList" and verb == "DELETE":
            for l in list(self.lists):
                self.btc_free += l["qty"]; self.btc_locked -= l["qty"]; self.lists.remove(l)
            return {"orderListId": int(q["orderListId"])}
        if path == "/api/v3/openOrders": return []
        raise AssertionError(f"unrouted {verb} {path}")

    def get(self, url, **kw): return Resp(self._route("GET", url))
    def post(self, url, data=None, **kw): return Resp(self._route("POST", url, data))
    def delete(self, url, **kw): return Resp(self._route("DELETE", url))


def gw(ex, **kw):
    s = Settings(_env_file=None, symbol="BTC/USD", binance_api_key="k", binance_api_secret="s",
                 paper_trading=False, dry_run=False, allow_live_trading=True,
                 live_risk_acknowledgement="I_ACCEPT_LIVE_TRADING_RISK",
                 stop_loss_pct=0.04, take_profit_pct=0.08, **kw)
    return BinanceGateway(s, session=ex, allow_order_submission=True)


def test_entry_places_oco_with_correct_prices_and_locks_quantity():
    ex = FakeEx(60000.0); g = gw(ex)
    assert g.buy_notional(25.0, userref=1) == "11"
    oco = [q for v, p, q in ex.log if p == "/api/v3/order/oco"][0]
    assert oco["side"] == "SELL" and oco["listClientOrderId"].startswith("dublin-oco-")
    assert Decimal(oco["stopPrice"]) == Decimal("57600.00")          # -4%
    assert Decimal(oco["price"]) == Decimal("64800.00")              # +8%
    assert Decimal(oco["stopLimitPrice"]) < Decimal(oco["stopPrice"])
    assert Decimal(oco["quantity"]) == Decimal("0.00041")            # 25/60000 rounded down to step
    assert ex.btc_locked == Decimal("0.00041") and ex.btc_free == 0   # protected, nothing loose


def test_exit_cancels_oco_then_sells_and_ends_flat():
    ex = FakeEx(); g = gw(ex); g.buy_notional(25.0, userref=1)
    assert g.close_position(userref=2, quantity=0.00041) == "12"
    assert ex.lists == [] and ex.btc_free == 0 and ex.btc_locked == 0
    kinds = [p for v, p, q in ex.log if p in ("/api/v3/orderList", "/api/v3/order")]
    assert kinds.index("/api/v3/orderList") < len(kinds) - 1          # cancel came before the sell


def test_oco_rejected_flattens_the_entry_and_raises():
    ex = FakeEx(oco_fails=True); g = gw(ex)
    with pytest.raises(BrokerError, match="entry flattened"):
        g.buy_notional(25.0, userref=1)
    assert ex.btc_free == 0 and ex.btc_locked == 0                   # never left holding unprotected


def test_oco_and_flatten_both_fail_is_loud():
    ex = FakeEx(oco_fails=True, sell_fails=True); g = gw(ex)
    with pytest.raises(BrokerError, match="UNPROTECTED"):
        g.buy_notional(25.0, userref=1)


def test_close_when_stop_already_filled_is_a_noop():
    ex = FakeEx(); g = gw(ex)                                         # nothing held, nothing resting
    assert g.close_position(userref=3, quantity=0.001) == "binance-no-position"


def test_exchange_stop_can_be_disabled():
    ex = FakeEx(); g = gw(ex, binance_exchange_stop=False); g.buy_notional(25.0, userref=1)
    assert not any(p == "/api/v3/order/oco" for _, p, _ in ex.log)
    assert ex.btc_free == Decimal("0.00041")


def test_close_sells_in_exact_lot_steps():
    ex = FakeEx(); g = gw(ex, binance_exchange_stop=False); g.buy_notional(25.0, userref=1)
    g.close_position(userref=2)                                       # whole balance
    sell = [q for v, p, q in ex.log if p == "/api/v3/order" and q.get("side") == "SELL"][0]
    assert sell["quantity"] == "0.00041"

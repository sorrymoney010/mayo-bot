"""Offline regression tests for the Binance adapter (fake HTTP session, no network)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from dublin_bot.binance_gateway import BinanceGateway, _decimals
from dublin_bot.config import Settings


class Resp:
    def __init__(self, data): self._d = data
    def json(self): return self._d
    def raise_for_status(self): pass


class FakeSession:
    def __init__(self, routes):
        self.routes, self.calls = routes, []
    def _hit(self, verb, url, **kw):
        self.calls.append((verb, url.split("?")[0].replace("https://api.binance.us", "")))
        for (v, path), data in self.routes.items():
            if v == verb and url.split("?")[0].endswith(path):
                return Resp(data)
        raise AssertionError(f"unrouted {verb} {url}")
    def get(self, url, **kw): return self._hit("GET", url, **kw)
    def post(self, url, **kw): return self._hit("POST", url, **kw)
    def delete(self, url, **kw): return self._hit("DELETE", url, **kw)


def gw(routes, live=False):
    s = Settings(symbol="BTC/USD", binance_api_key="k", binance_api_secret="s",
                 paper_trading=not live, dry_run=not live, allow_live_trading=live,
                 live_risk_acknowledgement="I_ACCEPT_LIVE_TRADING_RISK" if live else "")
    return BinanceGateway(s, session=FakeSession(routes), allow_order_submission=live)


def test_decimals():
    assert _decimals(Decimal("0.00100000")) == 3
    assert _decimals(Decimal("1.00000000")) == 0
    assert _decimals(Decimal("0.01000000")) == 2


def test_cancel_uses_delete_not_post():
    g = gw({("DELETE", "/api/v3/order"): {"orderId": 1}}, live=True)
    assert g.cancel_order("1") is True
    assert g._session.calls == [("DELETE", "/api/v3/order")]


def test_positions_skip_dust_and_fee_assets_and_report_entry():
    routes = {
        ("GET", "/api/v3/account"): {"balances": [
            {"asset": "USDT", "free": "50", "locked": "0"},
            {"asset": "BNB", "free": "0.5", "locked": "0"},
            {"asset": "DOGE", "free": "2", "locked": "0"},      # dust
            {"asset": "BTC", "free": "0.01", "locked": "0"}]},
        ("GET", "/api/v3/ticker/price"): [
            {"symbol": "BTCUSDT", "price": "60000"}, {"symbol": "DOGEUSDT", "price": "0.1"},
            {"symbol": "BNBUSDT", "price": "600"}],
        ("GET", "/api/v3/myTrades"): [
            {"time": 1, "qty": "0.005", "price": "50000", "isBuyer": True},
            {"time": 2, "qty": "0.005", "price": "58000", "isBuyer": True}],
    }
    pos = gw(routes).positions()
    assert [p["symbol"] for p in pos] == ["BTC/USD"]
    assert pos[0]["average_entry"] == pytest.approx(54000)
    assert gw(routes).has_position() is True


def test_dust_only_is_not_a_position():
    routes = {("GET", "/api/v3/account"): {"balances": [
        {"asset": "BNB", "free": "1", "locked": "0"}, {"asset": "USDT", "free": "9", "locked": "0"}]},
        ("GET", "/api/v3/ticker/price"): [{"symbol": "BNBUSDT", "price": "600"}]}
    assert gw(routes).has_position() is False


def test_find_order_by_userref_matches_client_id():
    routes = {("GET", "/api/v3/openOrders"): [
        {"orderId": 9, "symbol": "BTCUSDT", "side": "SELL", "status": "NEW", "clientOrderId": "dublin-42"},
        {"orderId": 8, "symbol": "BTCUSDT", "side": "SELL", "status": "NEW", "clientOrderId": "dublin-4"}]}
    g = gw(routes)
    assert g.find_order_by_userref(42)["order_id"] == "9"
    assert g.find_order_by_userref(2) is None


def test_binance_host_setting():
    s = Settings(binance_host="binance.com")
    assert BinanceGateway(s)._host == "https://api.binance.com"
    assert BinanceGateway(Settings())._host == "https://api.binance.us"


def test_size_buy_uses_step_size():
    routes = {("GET", "/api/v3/exchangeInfo"): {"symbols": [{
        "baseAsset": "BTC", "quoteAsset": "USDT", "filters": [
            {"filterType": "LOT_SIZE", "stepSize": "0.00010000", "minQty": "0.00010000"},
            {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
            {"filterType": "NOTIONAL", "minNotional": "5"}]}]}}
    sized = gw(routes).size_buy(100.0, price=60000)
    assert sized.volume == Decimal("0.0016")      # 0.001666.. rounded DOWN to step
    meta = gw(routes).resolve_symbol()
    assert (meta.lot_decimals, meta.pair_decimals) == (4, 2)


def test_live_add_order_refuses_kraken_format_bracket():
    from dublin_bot.errors import BrokerError
    g = gw({}, live=True)
    with pytest.raises(BrokerError, match="USE_BRACKET=false"):
        g.add_order({"pair": "BTCUSDT", "type": "buy", "ordertype": "market", "volume": "0.001"},
                    signal_price=60000.0)


def test_dry_add_order_records_intent_and_accepts_signal_price():
    g = gw({})
    assert g.add_order({"pair": "BTCUSDT", "type": "buy"}, signal_price=1.0).startswith("binance-dry-")


def test_buy_and_close_accept_signal_price_in_dry_run():
    g = gw({})
    assert g.buy_notional(25.0, userref=1, signal_price=60000.0).startswith("binance-dry-buy")
    assert g.close_position(userref=2, quantity=0.001, signal_price=60000.0).startswith("binance-dry-sell")


def test_cancel_orders_true_only_when_gone():
    open_orders = [{"orderId": 5, "symbol": "BTCUSDT", "side": "SELL", "status": "NEW", "clientOrderId": "x"}]
    g = gw({("DELETE", "/api/v3/order"): {}, ("GET", "/api/v3/openOrders"): open_orders}, live=True)
    assert g.cancel_orders(["5"]) is False      # still resting -> not confirmed
    assert g.cancel_orders(["9"]) is True       # a different id that is not on the book
    assert g.cancel_orders([]) is False


def test_balances_maps_free_plus_locked():
    g = gw({("GET", "/api/v3/account"): {"balances": [
        {"asset": "BTC", "free": "0.1", "locked": "0.05"}, {"asset": "ETH", "free": "0", "locked": "0"}]}})
    assert g.balances() == {"BTC": pytest.approx(0.15)}

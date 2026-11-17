"""Binance (Binance.US) gateway — fully offline.

Payloads below mirror real Binance.US responses (exchangeInfo filters, kline
rows, 24hr ticker, FULL order responses) captured from api.binance.us. A
``FakeSession`` serves them; no test touches the network or places an order.
Set ``BINANCE_LIVE_TESTS=1`` to also run the public-endpoint smoke test.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.parse
from decimal import Decimal

import pandas as pd
import pytest

from dublin_bot.binance_gateway import (
    BinanceGateway,
    classify_binance_error,
    client_id_for,
    interval_for,
    userref_from_client_id,
)
from dublin_bot.config import Settings
from dublin_bot.errors import (
    AuthenticationError,
    BrokerError,
    InsufficientFundsError,
    InvalidRequestError,
    PaperPrivateCallBlocked,
    PrecisionError,
    RateLimitError,
    SafetyLockError,
    TransientBrokerError,
)

SECRET = "binance-test-secret"
NOW = 1_794_945_984.0  # fixed clock (s)
HOUR_MS = 3_600_000


def _sym(symbol, base, quote, step, min_qty, tick, status="TRADING"):
    return {
        "symbol": symbol, "status": status, "baseAsset": base, "quoteAsset": quote,
        "orderTypes": ["LIMIT", "LIMIT_MAKER", "MARKET", "STOP_LOSS", "STOP_LOSS_LIMIT",
                       "TAKE_PROFIT", "TAKE_PROFIT_LIMIT"],
        "filters": [
            {"filterType": "PRICE_FILTER", "minPrice": "0.01000000", "maxPrice": "1000000.00000000",
             "tickSize": tick},
            {"filterType": "LOT_SIZE", "minQty": min_qty, "maxQty": "9000.00000000", "stepSize": step},
            {"filterType": "MIN_NOTIONAL", "minNotional": "1.00000000", "applyToMarket": True,
             "avgPriceMins": 5},
        ],
    }


EXCHANGE_INFO = {"symbols": [
    _sym("BTCUSD", "BTC", "USD", "0.00001000", "0.00001000", "0.01000000"),
    _sym("BTCUSDT", "BTC", "USDT", "0.00001000", "0.00001000", "0.01000000"),
    _sym("ETHUSD", "ETH", "USD", "0.00010000", "0.00010000", "0.01000000"),
    _sym("SOLUSD", "SOL", "USD", "0.00100000", "0.00100000", "0.01000000"),
    _sym("PUMPUSDT", "PUMP", "USDT", "1.00", "1.00", "0.0000001"),
    _sym("OLDUSD", "OLD", "USD", "1.00", "1.00", "0.01", status="BREAK"),
]}


def _kline(open_ms, close=63700.0):
    return [open_ms, f"{close - 10:.2f}", f"{close + 50:.2f}", f"{close - 60:.2f}", f"{close:.2f}",
            "2.00000000", open_ms + HOUR_MS - 1, f"{2 * close:.8f}", 100, "1.0", "1.0", "0"]


TICKER = {"symbol": "BTCUSD", "priceChangePercent": "-2.016", "weightedAvgPrice": "64507.43",
          "lastPrice": "63718.89", "lastQty": "0.00153", "bidPrice": "63699.93", "askPrice": "63700.56",
          "openPrice": "65029.96", "highPrice": "65484.49", "lowPrice": "63303.36", "volume": "52.5",
          "quoteVolume": "3387822.88", "count": 24351}


class FakeResp:
    def __init__(self, payload, status=200, headers=None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {"X-MBX-USED-WEIGHT-1M": "12"}
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    """Routes (METHOD, path) to handlers; records every call."""

    def __init__(self):
        self.headers = {}
        self.calls: list[dict] = []
        self.routes: dict[tuple[str, str], object] = {}
        self.route("GET", "/api/v3/exchangeInfo", EXCHANGE_INFO)
        self.route("GET", "/api/v3/time", {"serverTime": int(NOW * 1000)})
        self.route("GET", "/api/v3/ticker/24hr", TICKER)
        self.route("GET", "/api/v3/ping", {})

    def route(self, method, path, handler):
        self.routes[(method, path)] = handler

    def _dispatch(self, method, url, params=None, data=None, headers=None):
        parsed = urllib.parse.urlparse(url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        if params:
            query.update({k: str(v) for k, v in params.items()})
        if data:
            query.update(dict(urllib.parse.parse_qsl(data)))
        call = {"method": method, "path": parsed.path, "params": query, "headers": headers or {},
                "raw": data if data else parsed.query}
        self.calls.append(call)
        handler = self.routes.get((method, parsed.path))
        if handler is None:
            return FakeResp({"code": -1121, "msg": f"no route {method} {parsed.path}"}, 400)
        out = handler(call) if callable(handler) else handler
        return out if isinstance(out, FakeResp) else FakeResp(out)

    def get(self, url, params=None, headers=None, timeout=None):
        return self._dispatch("GET", url, params=params, headers=headers)

    def post(self, url, data=None, headers=None, timeout=None):
        return self._dispatch("POST", url, data=data, headers=headers)

    def delete(self, url, params=None, headers=None, timeout=None):
        return self._dispatch("DELETE", url, params=params, headers=headers)

    def paths(self, method=None):
        return [c["path"] for c in self.calls if method is None or c["method"] == method]


def _settings(**kw) -> Settings:
    base = dict(_env_file=None, broker="binance", symbol="BTC/USD", timeframe_minutes=60,
                lookback_bars=220, binance_api_key="k", binance_api_secret=SECRET)
    base.update(kw)
    return Settings(**base)


def _live_settings(**kw) -> Settings:
    return _settings(paper_trading=False, dry_run=False, allow_live_trading=True,
                     live_risk_acknowledgement="I_ACCEPT_LIVE_TRADING_RISK", **kw)


def _gw(settings=None, session=None, **kw) -> tuple[BinanceGateway, FakeSession]:
    session = session or FakeSession()
    gw = BinanceGateway(settings or _settings(), session=session, sleep_fn=lambda _s: None,
                        time_fn=lambda: NOW, **kw)
    return gw, session


# ── config ──────────────────────────────────────────────────────
@pytest.mark.parametrize("raw", ["binance", "Binance.US", "binanceus", "binance-us", "BINANCE"])
def test_broker_aliases_normalize(raw):
    assert Settings(_env_file=None, broker=raw).broker == "binance"


def test_has_credentials_follows_active_broker():
    assert Settings(_env_file=None, broker="binance", binance_api_key="a", binance_api_secret="b").has_credentials
    assert not Settings(_env_file=None, broker="binance", kraken_api_key="a", kraken_api_secret="b").has_credentials
    assert Settings(_env_file=None, broker="kraken", kraken_api_key="a", kraken_api_secret="b").has_credentials


def test_default_host_is_binance_us():
    gw, _ = _gw()
    assert gw.host == "https://api.binance.us"
    assert BinanceGateway(_settings(binance_host="binance.com"), session=FakeSession()).host == \
        "https://api.binance.com"


def test_build_gateway_selects_binance():
    from dublin_bot.engine import build_gateway
    assert isinstance(build_gateway(_settings(), session=FakeSession()), BinanceGateway)


# ── symbols ─────────────────────────────────────────────────────
def test_resolve_prefers_real_usd_pair_on_binance_us():
    gw, _ = _gw()
    meta = gw.resolve_symbol("BTC/USD")
    assert (meta.key, meta.base, meta.quote, meta.wsname) == ("BTCUSD", "BTC", "USD", "BTC/USD")
    assert meta.lot_decimals == 5 and meta.pair_decimals == 2
    assert meta.order_min == Decimal("0.00001") and meta.cost_min == Decimal("1")
    assert meta.tradable


def test_resolve_falls_back_to_usdt_when_no_usd_pair():
    gw, _ = _gw()
    meta = gw.resolve_symbol("PUMP/USD")
    assert meta.key == "PUMPUSDT" and meta.wsname == "PUMP/USD" and meta.lot_decimals == 0


def test_resolve_quote_preference_is_configurable():
    gw, _ = _gw(_settings(binance_usd_quotes=["USDT", "USD"]))
    assert gw.resolve_symbol("BTC/USD").key == "BTCUSDT"


def test_resolve_accepts_kraken_and_exchange_spellings():
    gw, _ = _gw()
    assert gw.resolve_symbol("XBT/USD").key == "BTCUSD"
    assert gw.resolve_symbol("BTCUSD").key == "BTCUSD"
    assert gw.resolve_symbol("XBTUSD").key == "BTCUSD"
    assert gw.resolve_symbol("BTCUSD:123").key == "BTCUSD"


def test_resolve_unknown_symbol_raises():
    gw, _ = _gw()
    with pytest.raises(InvalidRequestError):
        gw.resolve_symbol("NOPE/USD")


def test_list_usd_pairs_one_per_base_trading_only():
    gw, _ = _gw()
    keys = {m.key for m in gw.list_usd_pairs()}
    assert keys == {"BTCUSD", "ETHUSD", "SOLUSD", "PUMPUSDT"}


# ── market data ─────────────────────────────────────────────────
@pytest.mark.parametrize("mins,code", [(1, "1m"), (15, "15m"), (60, "1h"), (240, "4h"),
                                       (1440, "1d"), (45, "30m")])
def test_interval_mapping(mins, code):
    assert interval_for(mins)[1] == code


def test_get_bars_drops_forming_candle_and_uses_binance_interval():
    gw, session = _gw()
    now_ms = int(NOW * 1000)
    last_open = now_ms - now_ms % HOUR_MS  # currently forming
    rows = [_kline(last_open - i * HOUR_MS) for i in range(300)][::-1]
    session.route("GET", "/api/v3/klines", rows)
    bars = gw.get_bars()
    call = next(c for c in session.calls if c["path"] == "/api/v3/klines")
    assert call["params"]["interval"] == "1h" and call["params"]["symbol"] == "BTCUSD"
    assert len(bars) == 220
    assert bars.index[-1] == pd.Timestamp(last_open - HOUR_MS, unit="ms", tz="UTC")
    assert {"open", "high", "low", "close", "volume", "vwap", "trades"} <= set(bars.columns)
    assert bars.index.is_monotonic_increasing


def test_get_bars_pages_beyond_1000():
    gw, session = _gw(_settings(lookback_bars=1500))
    now_ms = int(NOW * 1000)
    last_open = now_ms - now_ms % HOUR_MS

    def klines(call):
        end = int(call["params"].get("endTime", last_open + HOUR_MS - 1))
        limit = int(call["params"]["limit"])
        top = end - (end % HOUR_MS)
        return [_kline(top - i * HOUR_MS) for i in range(limit)][::-1]

    session.route("GET", "/api/v3/klines", klines)
    bars = gw.get_bars()
    assert len(bars) == 1500
    assert sum(1 for p in session.paths() if p == "/api/v3/klines") == 2
    assert bars.index.is_unique


def test_ticker_and_market_quality():
    gw, _ = _gw()
    t = gw.get_ticker_for("BTC/USD")
    assert t["last"] == 63718.89 and t["bid"] == 63699.93 and t["ask"] == 63700.56
    assert t["high_24h"] == 65484.49 and t["trades_24h"] == 24351
    q = gw.market_quality()
    assert 0 < q["spread_bps"] < 1
    assert q["recent_dollar_volume"] == pytest.approx(3387822.88)


def test_freshness_uses_server_time():
    gw, session = _gw()
    now_ms = int(NOW * 1000)
    last_open = now_ms - now_ms % HOUR_MS
    session.route("GET", "/api/v3/klines", [_kline(last_open - i * HOUR_MS) for i in range(230)][::-1])
    verdict = gw.check_freshness(gw.get_bars())
    assert verdict.clock_skew_seconds is not None


def test_health_reports_venue():
    gw, _ = _gw()
    h = gw.health(include_freshness=False)
    assert h["broker"] == "binance" and h["reachable"] is True and h["venue"] == "https://api.binance.us"


# ── sizing ──────────────────────────────────────────────────────
def test_size_buy_rounds_down_to_step():
    gw, _ = _gw()
    sized = gw.size_buy(25.0, price=63700.56)
    assert sized.pair == "BTCUSD"
    assert sized.volume == Decimal("0.00039")
    assert sized.notional <= Decimal("25")


def test_size_buy_below_minimum_raises():
    gw, _ = _gw()
    with pytest.raises(PrecisionError):
        gw.size_buy(0.10, price=63700.56)


# ── safety locks ────────────────────────────────────────────────
def test_paper_mode_blocks_every_signed_call_even_with_keys():
    gw, session = _gw()  # default settings are paper + dry-run
    with pytest.raises(PaperPrivateCallBlocked):
        gw.balances()
    with pytest.raises(PaperPrivateCallBlocked):
        gw._private("Balance")
    assert not any("X-MBX-APIKEY" in c["headers"] for c in session.calls)


def test_dry_run_orders_never_reach_exchange():
    gw, session = _gw()
    assert gw.buy_notional(25.0, userref=7).startswith("binance-dry-buy-7")
    assert gw.close_position(quantity=0.001, userref=8).startswith("binance-dry-close-8")
    assert gw.add_order({"pair": "BTCUSD", "type": "buy", "ordertype": "market",
                         "volume": "0.001"}).startswith("binance-dry-")
    assert session.paths("POST") == [] and session.paths("DELETE") == []


def test_submission_needs_constructor_flag_too():
    gw, session = _gw(_live_settings(), allow_order_submission=False)
    assert not gw.order_submission_enabled
    gw.buy_notional(25.0, userref=1)
    assert session.paths("POST") == []
    with pytest.raises(SafetyLockError):
        gw._assert_can_submit()


def test_close_position_refuses_full_sweep():
    gw, _ = _gw(_live_settings(), allow_order_submission=True)
    with pytest.raises(BrokerError):
        gw.close_position(quantity=None)


def test_kraken_mutation_endpoints_refused():
    gw, _ = _gw(_live_settings(), allow_order_submission=True)
    for endpoint in ("AddOrder", "CancelOrder", "EditOrder"):
        with pytest.raises(SafetyLockError):
            gw._private(endpoint, {})


# ── live execution (fake exchange) ──────────────────────────────
def _verify_signature(call):
    raw = call["raw"]
    query, _, sig = raw.rpartition("&signature=")
    assert hmac.new(SECRET.encode(), query.encode(), hashlib.sha256).hexdigest() == sig
    assert call["headers"]["X-MBX-APIKEY"] == "k"


def _order_resp(call, *, order_id=111, status="FILLED", price="63700.00"):
    p = call["params"]
    qty = p.get("quantity", "0")
    executed = qty if status == "FILLED" else "0"
    cost = f"{float(executed) * float(price):.8f}"
    return {"symbol": p["symbol"], "orderId": order_id, "clientOrderId": p.get("newClientOrderId", "x"),
            "transactTime": int(NOW * 1000), "price": "0", "origQty": qty, "executedQty": executed,
            "cummulativeQuoteQty": cost, "status": status, "type": p["type"], "side": p["side"],
            "fills": ([{"price": price, "qty": executed, "commission": "0.00000039",
                        "commissionAsset": "BTC"}] if status == "FILLED" else [])}


def test_live_market_buy_is_signed_sized_and_fill_captured():
    gw, session = _gw(_live_settings(), allow_order_submission=True)
    session.route("POST", "/api/v3/order", lambda c: _order_resp(c))
    txid = gw.buy_notional(25.0, userref=4242, signal_price=63690.0)
    assert txid == "BTCUSD:111"
    post = next(c for c in session.calls if c["method"] == "POST")
    _verify_signature(post)
    p = post["params"]
    assert (p["symbol"], p["side"], p["type"], p["quantity"]) == ("BTCUSD", "BUY", "MARKET", "0.00039")
    assert p["newClientOrderId"] == "dublin-4242"
    assert p["newOrderRespType"] == "FULL"
    fill = gw.last_fill
    assert fill is not None and fill.order_id == "BTCUSD:111" and fill.fill_price == pytest.approx(63700.0)
    assert fill.fee == pytest.approx(0.00000039 * 63700.0)


def test_live_close_caps_at_free_balance():
    gw, session = _gw(_live_settings(), allow_order_submission=True)
    session.route("GET", "/api/v3/account", {"balances": [
        {"asset": "BTC", "free": "0.00050", "locked": "0"}, {"asset": "USD", "free": "10", "locked": "0"}]})
    session.route("POST", "/api/v3/order", lambda c: _order_resp(c, order_id=222))
    assert gw.close_position(quantity=0.01, userref=9) == "BTCUSD:222"
    post = next(c for c in session.calls if c["method"] == "POST")
    assert post["params"]["side"] == "SELL" and post["params"]["quantity"] == "0.0005"


def test_bracket_market_entry_arms_stop_and_reports_closetxid():
    gw, session = _gw(_live_settings(), allow_order_submission=True)
    ids = iter([500, 501])
    session.route("POST", "/api/v3/order", lambda c: _order_resp(c, order_id=next(ids)))
    session.route("GET", "/api/v3/account", {"balances": [{"asset": "BTC", "free": "0.001", "locked": "0"}]})
    from dublin_bot.orders import BracketPlan
    plan = BracketPlan(pair="BTCUSD", side="buy", volume="0.00100", stop_loss=61000.123, userref=300,
                       pair_decimals=2)
    parent = gw.add_order(plan.to_addorder_params(), signal_price=63700.0)
    posts = [c for c in session.calls if c["method"] == "POST"]
    assert parent == "BTCUSD:500" and len(posts) == 2
    stop = posts[1]["params"]
    assert (stop["type"], stop["side"], stop["stopPrice"]) == ("STOP_LOSS", "SELL", "61000.12")
    assert stop["newClientOrderId"] == "dublin-301"
    assert float(stop["quantity"]) <= 0.001

    session.route("GET", "/api/v3/order", {"symbol": "BTCUSD", "orderId": 500, "status": "FILLED",
                                           "clientOrderId": "dublin-300", "origQty": "0.001",
                                           "executedQty": "0.001", "cummulativeQuoteQty": "63.7",
                                           "type": "MARKET", "side": "BUY", "price": "0", "time": 1})
    session.route("GET", "/api/v3/myTrades", [])
    entry = gw.query_order(parent)
    assert entry["closetxid"] == "BTCUSD:501"
    assert entry["status"] == "closed" and entry["vol_exec"] == 0.001
    assert entry["descr"] == {"pair": "BTCUSD", "type": "buy", "ordertype": "market", "price": "0"}
    assert entry["price"] == pytest.approx(63700.0) and entry["userref"] == 300


def test_limit_and_take_profit_translation():
    gw, session = _gw(_live_settings(), allow_order_submission=True)
    session.route("POST", "/api/v3/order", lambda c: _order_resp(c, status="NEW"))
    from dublin_bot.orders import build_take_profit_order
    gw.add_order(build_take_profit_order("BTCUSD", "buy", "0.00100", 70000.129, userref=302))
    gw.add_order({"pair": "BTCUSD", "type": "buy", "ordertype": "limit", "price": "63000.009",
                  "volume": "0.001234567", "oflags": "post"})
    tp, mk = [c["params"] for c in session.calls if c["method"] == "POST"]
    assert (tp["type"], tp["side"], tp["price"], tp["timeInForce"]) == ("LIMIT", "SELL", "70000.12", "GTC")
    assert tp["newClientOrderId"] == "dublin-302"
    assert (mk["type"], mk["price"], mk["quantity"]) == ("LIMIT_MAKER", "63000", "0.00123")


def test_cancel_uses_delete_with_symbol_and_id():
    gw, session = _gw(_live_settings(), allow_order_submission=True)
    session.route("DELETE", "/api/v3/order", {"status": "CANCELED"})
    session.route("GET", "/api/v3/openOrders", [])
    assert gw.cancel_orders(["SOLUSD:77"]) is True
    d = next(c for c in session.calls if c["method"] == "DELETE")
    assert d["params"]["symbol"] == "SOLUSD" and d["params"]["orderId"] == "77"
    _verify_signature(d)


def test_find_order_by_userref_checks_history():
    gw, session = _gw(_live_settings(), allow_order_submission=True)
    session.route("GET", "/api/v3/openOrders", [])

    def order(call):
        if call["params"].get("origClientOrderId") == "dublin-55" and call["params"]["symbol"] == "BTCUSD":
            return {"symbol": "BTCUSD", "orderId": 9, "clientOrderId": "dublin-55", "status": "FILLED",
                    "origQty": "0.001", "executedQty": "0.001", "cummulativeQuoteQty": "63",
                    "type": "MARKET", "side": "BUY", "price": "0"}
        return FakeResp({"code": -2013, "msg": "Order does not exist."}, 400)

    session.route("GET", "/api/v3/order", order)
    found = gw.find_order_by_userref(55)
    assert found["id"] == "BTCUSD:9" and found["userref"] == 55 and found["status"] == "closed"


def test_closed_trade_pnl_average_cost():
    gw, session = _gw(_live_settings(), allow_order_submission=True)

    def trades(call):
        if call["params"]["symbol"] != "BTCUSD":
            return []
        return [
            {"id": 1, "orderId": 1, "price": "100", "qty": "1", "quoteQty": "100", "commission": "0",
             "commissionAsset": "USD", "time": 1000_000, "isBuyer": True},
            {"id": 2, "orderId": 2, "price": "200", "qty": "1", "quoteQty": "200", "commission": "0",
             "commissionAsset": "USD", "time": 2000_000, "isBuyer": True},
            {"id": 3, "orderId": 3, "price": "300", "qty": "1", "quoteQty": "300", "commission": "1",
             "commissionAsset": "USD", "time": 3000_000, "isBuyer": False},
        ]

    session.route("GET", "/api/v3/myTrades", trades)
    rows, cursor = gw.closed_trade_pnl(since=0)
    assert rows == [{"txid": "BTCUSD-3", "symbol": "BTCUSD", "pnl": 149.0, "ts": 3000}]
    assert cursor == 3000
    assert gw.closed_trade_pnl(since=3001)[0] == []


def test_fee_schedule_in_bps():
    gw, session = _gw(_live_settings(), allow_order_submission=True)
    session.route("GET", "/sapi/v1/asset/query/trading-fee",
                  [{"symbol": "BTCUSD", "makerCommission": "0.004", "takerCommission": "0.006"}])
    assert gw.fee_schedule() == {"taker_bps": pytest.approx(60.0), "maker_bps": pytest.approx(40.0)}


def test_account_equity_marks_holdings():
    gw, session = _gw(_live_settings(), allow_order_submission=True)
    session.route("GET", "/api/v3/account", {"balances": [
        {"asset": "USD", "free": "10", "locked": "0"}, {"asset": "USDT", "free": "5", "locked": "0"},
        {"asset": "BTC", "free": "0.001", "locked": "0.001"}, {"asset": "PUMP", "free": "100", "locked": "0"}]})
    session.route("GET", "/api/v3/ticker/price", [{"symbol": "BTCUSD", "price": "60000"},
                                                  {"symbol": "PUMPUSDT", "price": "0.01"}])
    assert gw.account_equity() == pytest.approx(10 + 5 + 120 + 1)
    pos = gw.positions()
    assert pos[0]["asset"] == "BTC" and pos[0]["quantity"] == pytest.approx(0.002)


# ── transport ───────────────────────────────────────────────────
def test_error_classification():
    assert isinstance(classify_binance_error(429, -1003, "Too many requests"), RateLimitError)
    assert isinstance(classify_binance_error(400, -2015, "Invalid API-key"), AuthenticationError)
    assert isinstance(classify_binance_error(400, -2010, "Account has insufficient balance"),
                      InsufficientFundsError)
    assert isinstance(classify_binance_error(400, -1021, "Timestamp outside recvWindow"),
                      TransientBrokerError)
    assert isinstance(classify_binance_error(400, -1013, "Filter failure: LOT_SIZE"), InvalidRequestError)
    assert isinstance(classify_binance_error(503, None, "busy"), TransientBrokerError)


def test_reads_retry_rate_limits_then_succeed():
    gw, session = _gw()
    replies = iter([FakeResp({"code": -1003, "msg": "slow down"}, 429), FakeResp(TICKER)])
    session.route("GET", "/api/v3/ticker/24hr", lambda c: next(replies))
    assert gw.get_ticker()["last"] == 63718.89


def test_orders_are_not_retried_on_transient_error():
    gw, session = _gw(_live_settings(), allow_order_submission=True)
    session.route("POST", "/api/v3/order", FakeResp({"code": -1001, "msg": "Internal error"}, 503))
    with pytest.raises(TransientBrokerError):
        gw.buy_notional(25.0, userref=1)
    assert len(session.paths("POST")) == 1


def test_timestamp_uses_server_offset():
    session = FakeSession()
    session.route("GET", "/api/v3/time", {"serverTime": int(NOW * 1000) + 2500})
    session.route("GET", "/api/v3/account", {"balances": []})
    gw, _ = _gw(_live_settings(), session=session, allow_order_submission=True)
    gw.balances()
    acct = next(c for c in session.calls if c["path"] == "/api/v3/account")
    assert int(acct["params"]["timestamp"]) == int(NOW * 1000) + 2500
    assert acct["params"]["recvWindow"] == "5000"


def test_client_id_round_trip():
    assert client_id_for(12) == "dublin-12" and userref_from_client_id("dublin-12") == 12
    assert userref_from_client_id("web_abc") is None and client_id_for(None) is None


# ── Kraken-shaped compatibility (dashboard / research scripts) ──
def test_kraken_public_ohlc_shape():
    gw, session = _gw()
    session.route("GET", "/api/v3/klines", [_kline(1_000 * HOUR_MS), _kline(1_001 * HOUR_MS)])
    res = gw._public("OHLC", {"pair": "XBTUSD", "interval": 60, "since": "100"})
    rows = res["BTCUSD"]
    assert rows[0][0] == 1_000 * 3600 and len(rows[0]) == 8 and res["last"] == 1_001 * 3600
    call = next(c for c in session.calls if c["path"] == "/api/v3/klines")
    assert call["params"]["startTime"] == "100000"


def test_kraken_private_balance_shape():
    gw, session = _gw(_live_settings(), allow_order_submission=True)
    session.route("GET", "/api/v3/account", {"balances": [{"asset": "USD", "free": "1.5", "locked": "0.5"}]})
    assert gw._private("Balance") == {"USD": "2.0"}


# ── wiring outside the gateway ──────────────────────────────────
def test_futures_sleeve_disabled_on_binance():
    from dublin_bot.futures_sleeve import sleeve_active
    ok, reason = sleeve_active(_settings(futures_sleeve_enabled=True))
    assert ok is False and "Binance" in reason


def test_rotation_executor_refuses_binance():
    from dublin_bot.rotation_execution import RotationExecutor
    gw, _ = _gw()
    with pytest.raises(SafetyLockError):
        RotationExecutor(gw, _settings())


def test_venue_helpers_follow_broker(monkeypatch):
    from dublin_bot import venue
    gw, session = _gw()
    session.route("GET", "/api/v3/ticker/price", [{"symbol": "BTCUSD", "price": "60000"},
                                                  {"symbol": "SOLUSD", "price": "150"}])
    session.route("GET", "/api/v3/klines", [_kline(1_000 * HOUR_MS)])
    monkeypatch.setenv("BROKER", "binance.us")
    monkeypatch.setattr(venue, "_binance", gw)
    assert venue.is_binance()
    assert venue.public_prices(["BTC/USD", "SOL/USD", "NOPE/USD"]) == {"BTC/USD": 60000.0, "SOL/USD": 150.0}
    rows = venue.public_ohlc("BTC/USD", 1440)
    assert rows[0][0] == 1_000 * 3600 and float(rows[0][4]) == 63700.0
    call = [c for c in session.calls if c["path"] == "/api/v3/klines"][-1]
    assert call["params"]["interval"] == "1d"
    from dublin_bot.exit_watcher import _fresh_ticks, _rest_last
    assert _rest_last("SOL/USD") == 150.0
    assert _fresh_ticks("data", ("BTC/USD",), time.time()) == {}


def test_binance_ws_ingest():
    from dublin_bot.realtime import BinanceRealtimeFeed
    feed = BinanceRealtimeFeed({"BTC/USD": "BTCUSD"})
    assert feed.WS_URL == "wss://stream.binance.us:443/stream?streams=btcusd@ticker"
    feed._ingest(json.dumps({"stream": "btcusd@ticker",
                             "data": {"e": "24hrTicker", "s": "BTCUSD", "c": "63000.5", "b": "63000", "a": "63001"}}))
    snap = feed.latest("BTC/USD")
    assert (snap.last, snap.bid, snap.ask, snap.source) == (63000.5, 63000.0, 63001.0, "ws")


# ── optional live smoke test (public endpoints only) ────────────
@pytest.mark.skipif(os.environ.get("BINANCE_LIVE_TESTS") != "1", reason="set BINANCE_LIVE_TESTS=1")
def test_live_binance_us_public_smoke():
    gw = BinanceGateway(Settings(_env_file=None, broker="binance", timeframe_minutes=60, lookback_bars=220))
    assert gw.resolve_symbol("BTC/USD").key == "BTCUSD"
    bars = gw.get_bars()
    assert len(bars) == 220 and gw.check_freshness(bars).fresh
    assert gw.get_ticker()["last"] > 0
    assert gw.health(include_freshness=False)["reachable"]

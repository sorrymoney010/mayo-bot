"""Tests for the Kraken Spot gateway.

All HTTP calls are mocked — no live network access.
Covers: read-only market data, balances, positions, orders,
authentication / nonce errors, rate limiting, and
dry-run order placement.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import urllib.parse
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from dublin_bot.config import Settings
from dublin_bot.errors import (
    AuthenticationError,
    BrokerError,
    InvalidRequestError,
    RateLimitError,
)
from dublin_bot.kraken_gateway import KrakenGateway, SymbolMeta


def make_settings(**overrides) -> Settings:
    """Build a Settings instance in paper/dry-run mode with fake Kraken keys."""
    defaults = dict(
        _env_file=None,
        broker="kraken",
        paper_trading=True,
        dry_run=True,
        allow_live_trading=False,
        kraken_api_key="fake-key",
        kraken_api_secret="fake-secret",
        symbol="BTC/USD",
        timeframe_minutes=60,
        lookback_bars=500,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _mock_response(json_data: dict, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status.return_value = None
    return resp


def _make_meta() -> SymbolMeta:
    return SymbolMeta(
        key="XXBTZUSD",
        altname="XBTUSD",
        wsname="XBT/USD",
        base="XBT",
        quote="ZUSD",
        lot_decimals=8,
        pair_decimals=2,
        order_min=Decimal("0.01"),
        cost_min=Decimal("0.01"),
        status="online",
    )


def _make_gateway_with_creds(settings: Settings) -> KrakenGateway:
    """Build a gateway with credentials set (simulating real key entry)."""
    gw = KrakenGateway(settings)
    gw._api_key = "fake-key"
    gw._api_secret = base64.b64encode(b"x" * 32).decode()
    return gw


def _prime_metadata(gw: KrakenGateway, key: str = "XXBTZUSD") -> None:
    """Pre-populate the metadata cache so resolve_symbol() works without HTTP."""
    gw._meta = {key: _make_meta()}
    gw._meta_loaded_at = 9999999999.0


# ── Read-only: market data ──────────────────────────────────────────

def test_get_bars_returns_dataframe_with_required_columns():
    settings = make_settings()
    gw = _make_gateway_with_creds(settings)
    _prime_metadata(gw)

    ohlc_data = [
        [1700000000, "50000.0", "51000.0", "49000.0", "50500.0", "50000.0", "10.0", 100],
        [1700003600, "50500.0", "52000.0", "50000.0", "51500.0", "50750.0", "15.0", 110],
    ]
    mock_json = {"error": [], "result": {"XXBTZUSD": ohlc_data}}

    with patch.object(gw._session, "get", return_value=_mock_response(mock_json)):
        bars = gw.get_bars()

    assert isinstance(bars, pd.DataFrame)
    assert {"open", "high", "low", "close", "volume"}.issubset(bars.columns)
    # get_bars drops the last (in-progress) candle
    assert len(bars) == 1
    assert float(bars.iloc[0]["close"]) == 50500.0


def test_get_bars_raises_on_api_error():
    settings = make_settings()
    gw = _make_gateway_with_creds(settings)
    _prime_metadata(gw)

    with patch.object(gw, "_public", side_effect=BrokerError("EAPI:Invalid pair")):
        with pytest.raises(BrokerError, match="EAPI"):
            gw.get_bars()


def test_get_bars_raises_when_no_ohlc_data():
    settings = make_settings()
    gw = _make_gateway_with_creds(settings)
    _prime_metadata(gw)

    with patch.object(gw, "_public", return_value={"last": 123}):
        with pytest.raises(BrokerError, match="No OHLC data"):
            gw.get_bars()


# ── Read-only: ticker ───────────────────────────────────────────────

def test_get_ticker_returns_bid_ask_last():
    settings = make_settings()
    gw = _make_gateway_with_creds(settings)
    _prime_metadata(gw)

    mock_json = {
        "error": [],
        "result": {
            "XXBTZUSD": {
                "a": ["50000.0", "1", "1.0"],
                "b": ["49950.0", "1", "1.0"],
                "c": ["50000.0", "0.001", 1234567890],
                "v": ["10.0", "20.0"],
                "p": ["50000.0", "49500.0"],
                "t": [100, 200],
                "l": ["49000.0", "48500.0"],
                "h": ["51000.0", "51500.0"],
                "o": "50000.0",
            }
        },
    }
    with patch.object(gw, "_public", return_value=mock_json["result"]):
        ticker = gw.get_ticker()

    assert ticker["bid"] == 49950.0
    assert ticker["ask"] == 50000.0
    assert ticker["last"] == 50000.0
    assert ticker["volume_24h"] == 20.0
    assert ticker["vwap_24h"] == 49500.0


# ── Read-only: server time ──────────────────────────────────────────

def test_server_time_returns_unix_timestamp():
    settings = make_settings()
    gw = _make_gateway_with_creds(settings)

    with patch.object(gw, "_public", return_value={"unixtime": 1700000000.123}):
        ts = gw.server_time()
    assert ts == 1700000000.123


# ── Authentication ──────────────────────────────────────────────────

def test_private_call_raises_without_credentials():
    settings = make_settings(kraken_api_key="", kraken_api_secret="")
    gw = KrakenGateway(settings)

    with pytest.raises(AuthenticationError, match="Kraken credentials required"):
        gw._private("Balance")


def test_sign_produces_deterministic_hmac():
    settings = make_settings(
        kraken_api_key="mykey",
        kraken_api_secret=base64.b64encode(b"0" * 32).decode(),
    )
    gw = KrakenGateway(settings)

    params = {"nonce": "1616492376594", "pair": "XBTUSD"}
    sig = gw._sign("/0/private/AddOrder", params)

    # Independently recompute to verify determinism
    secret = base64.b64decode(settings.kraken_api_secret)
    encoded = (params["nonce"] + urllib.parse.urlencode(params)).encode()
    message = "/0/private/AddOrder".encode() + hashlib.sha256(encoded).digest()
    expected = base64.b64encode(hmac.new(secret, message, hashlib.sha512).digest()).decode()
    assert sig == expected


def test_sign_raises_on_invalid_base64_secret():
    settings = make_settings(kraken_api_key="mykey", kraken_api_secret="not-valid-base64!!!")
    gw = KrakenGateway(settings)
    params = {"nonce": "123"}
    with pytest.raises(AuthenticationError, match="not valid base64"):
        gw._sign("/0/private/Balance", params)


# ── Symbol metadata / precision ─────────────────────────────────────

def test_load_metadata_parses_asset_pairs_response():
    settings = make_settings()
    gw = _make_gateway_with_creds(settings)

    mock_json = {
        "error": [],
        "result": {
            "XXBTZUSD": {
                "pair": "XBT/USD",
                "altname": "XBTUSD",
                "wsname": "XBT/USD",
                "base": "XBT",
                "quote": "ZUSD",
                "lot": "0.000001",
                "lot_decimals": 8,
                "pair_decimals": 2,
                "ordermin": "0.0100000",
                "costmin": "0.0100000",
                "status": "online",
            }
        }
    }
    with patch.object(gw._session, "get", return_value=_mock_response(mock_json)):
        meta = gw.load_metadata(force=True)

    assert "XXBTZUSD" in meta
    entry = meta["XXBTZUSD"]
    assert entry.key == "XXBTZUSD"
    assert entry.altname == "XBTUSD"
    assert entry.pair_decimals == 2
    assert entry.lot_decimals == 8
    assert entry.order_min == Decimal("0.0100000")
    assert entry.tradable is True


def test_resolve_symbol_handles_btc_alias():
    """BTC/USD should resolve to XBTUSD via the BTC → XBT alias."""
    settings = make_settings(symbol="BTC/USD")
    gw = _make_gateway_with_creds(settings)
    _prime_metadata(gw)
    meta = gw.resolve_symbol()
    assert meta.key == "XXBTZUSD"
    assert meta.altname == "XBTUSD"


def test_resolve_symbol_raises_for_unknown_pair():
    settings = make_settings(symbol="DOGE/USD")
    gw = _make_gateway_with_creds(settings)
    _prime_metadata(gw)
    with pytest.raises(InvalidRequestError, match="asset pair matching"):
        gw.resolve_symbol()


def test_asset_info_returns_none_for_unknown_symbol():
    settings = make_settings(symbol="DOGE/USD")
    gw = _make_gateway_with_creds(settings)
    _prime_metadata(gw)
    assert gw.asset_info() is None


# ── Rate limiting ───────────────────────────────────────────────────

def test_rate_limit_error_is_raised_on_429():
    settings = make_settings()
    gw = _make_gateway_with_creds(settings)

    rate_limit_resp = MagicMock()
    rate_limit_resp.status_code = 429
    rate_limit_resp.raise_for_status.side_effect = Exception("429")

    with patch.object(gw._session, "post", return_value=rate_limit_resp):
        with pytest.raises(RateLimitError, match="429"):
            gw._private("Balance")


def test_transient_error_retried_up_to_max_retries():
    """5xx errors should be retried with backoff."""
    settings = make_settings(max_retries=3)
    gw = _make_gateway_with_creds(settings)
    gw._sleep = MagicMock()

    bad = MagicMock(status_code=503)
    bad.raise_for_status.side_effect = Exception("503")
    good = _mock_response({"error": [], "result": {"eb": "100.0"}})

    with patch.object(gw._session, "post", side_effect=[bad, bad, good]):
        result = gw._private("TradeBalance")
    assert result["eb"] == "100.0"
    assert gw._sleep.call_count == 2  # slept between retries


def test_authentication_error_is_not_retried():
    """Auth failures must fail fast — no retry loop."""
    settings = make_settings(max_retries=3)
    gw = _make_gateway_with_creds(settings)
    gw._sleep = MagicMock()

    mock_json = {"error": ["EAPI:Invalid key"], "result": {}}

    with patch.object(gw._session, "post", return_value=_mock_response(mock_json)):
        with pytest.raises(AuthenticationError, match="Invalid key"):
            gw._private("Balance")
    assert gw._sleep.call_count == 0


# ── HTTP error handling ─────────────────────────────────────────────

def test_network_timeout_raises_transient_error():
    import requests as req_lib
    settings = make_settings()
    gw = _make_gateway_with_creds(settings)

    with patch.object(gw._session, "get", side_effect=req_lib.Timeout("timed out")):
        with pytest.raises(BrokerError, match="timeout"):
            gw.get_bars()


# ── Read-only: balances, positions, orders ──────────────────────────

def test_account_equity_from_kraken_trade_balance():
    settings = make_settings()
    gw = _make_gateway_with_creds(settings)

    with patch.object(gw, "_private", return_value={"eb": "1250.50"}):
        equity = gw.account_equity()
    assert equity == 1250.50


def test_account_equity_falls_back_when_no_credentials():
    settings = make_settings(kraken_api_key="", kraken_api_secret="")
    gw = KrakenGateway(settings)
    assert gw.account_equity() == settings.strategy_equity_usd


def test_account_equity_falls_back_on_api_error():
    settings = make_settings()
    gw = _make_gateway_with_creds(settings)

    with patch.object(gw, "_private", side_effect=BrokerError("down")):
        equity = gw.account_equity()
    assert equity == settings.strategy_equity_usd  # graceful fallback


def test_balances_parses_kraken_balance_response():
    settings = make_settings()
    gw = _make_gateway_with_creds(settings)

    with patch.object(gw, "_private", return_value={
        "XXBT": "0.500", "ZUSD": "1000.00", "XXRP": "500.0"
    }):
        bal = gw.balances()

    assert bal == {"XXBT": 0.5, "ZUSD": 1000.0, "XXRP": 500.0}


def test_positions_reconstructs_from_balances():
    settings = make_settings()
    gw = _make_gateway_with_creds(settings)
    _prime_metadata(gw)

    with patch.object(gw, "balances", return_value={"XBT": 0.5}):
        with patch.object(gw, "get_ticker", return_value={"last": 50000.0}):
            positions = gw.positions()

    assert len(positions) == 1
    p = positions[0]
    assert p["symbol"] == "XBTUSD"
    assert p["side"] == "long"
    assert p["quantity"] == 0.5
    assert p["average_entry"] == 0.0  # spot has no cost basis
    assert p["market_value"] == 25000.0


def test_has_position_detects_open_positions():
    settings = make_settings()
    gw = _make_gateway_with_creds(settings)
    _prime_metadata(gw)

    with patch.object(gw, "positions", return_value=[{"symbol": "XBTUSD"}]):
        assert gw.has_position() is True
    with patch.object(gw, "positions", return_value=[]):
        assert gw.has_position() is False


def test_orders_parses_open_orders():
    settings = make_settings()
    gw = _make_gateway_with_creds(settings)

    with patch.object(gw, "_private", return_value={
        "open": {
            "ORDER456": {
                "pair": "XBTUSD",
                "descr": {"type": "buy", "pair": "XBTUSD", "ordertype": "market"},
                "cost": "10000.00",
                "status": "open",
                "vol": "0.2",
                "vol_exec": "0.0",
            }
        }
    }):
        orders = gw.orders()

    assert len(orders) == 1
    assert orders[0]["id"] == "ORDER456"
    assert orders[0]["symbol"] == "XBTUSD"
    assert orders[0]["side"] == "buy"
    assert orders[0]["status"] == "open"
    assert orders[0]["notional"] == 10000.0


def test_orders_returns_empty_when_no_credentials():
    settings = make_settings(kraken_api_key="", kraken_api_secret="")
    gw = KrakenGateway(settings)
    assert gw.orders() == []


def test_find_order_by_userref_searches_closed_orders():
    settings = make_settings()
    gw = _make_gateway_with_creds(settings)

    open_result = {"open": {}}
    closed_result = {
        "closed": {
            "TXID789": {
                "descr": {"type": "buy", "pair": "XBTUSD"},
                "cost": "5000.00",
                "status": "closed",
                "vol": "0.1",
                "vol_exec": "0.1",
                "userref": 42,
            }
        }
    }

    with patch.object(gw, "_private", side_effect=[open_result, closed_result]):
        found = gw.find_order_by_userref(42)

    assert found is not None
    assert found["id"] == "TXID789"
    assert found["status"] == "closed"


# ── Dry-run order execution ─────────────────────────────────────────

def test_buy_notional_dry_run_returns_synthetic_id():
    settings = make_settings()
    gw = _make_gateway_with_creds(settings)
    _prime_metadata(gw)

    sized = MagicMock(
        pair="XXBTZUSD", volume_str="0.01000000", price_str="50000.0",
        notional=500.0
    )
    with patch.object(gw, "size_buy", return_value=sized):
        order_id = gw.buy_notional(500.0, userref=42)
    assert "dry" in order_id.lower()


def test_close_position_dry_run_with_no_position_raises():
    settings = make_settings()
    gw = _make_gateway_with_creds(settings)
    _prime_metadata(gw)

    with patch.object(gw, "positions", return_value=[]):
        with pytest.raises(BrokerError, match="No position"):
            gw.close_position()


def test_close_position_dry_run_returns_synthetic_id():
    settings = make_settings()
    gw = _make_gateway_with_creds(settings)
    _prime_metadata(gw)

    with patch.object(gw, "positions", return_value=[{
        "quantity": 0.5, "symbol": "XBTUSD"
    }]):
        order_id = gw.close_position(quantity=0.5)
    assert "dry" in order_id.lower()


def test_close_position_refuses_none_quantity_no_sweep():
    """A None quantity would liquidate the ENTIRE balance — hard-refuse it.

    This is the no-sweep safeguard: an empty bot-owned ledger must never
    produce a full-balance sell of external/legacy holdings.
    """
    settings = make_settings()
    gw = _make_gateway_with_creds(settings)
    _prime_metadata(gw)

    with patch.object(gw, "positions", return_value=[{
        "quantity": 0.5, "symbol": "XBTUSD"
    }]):
        with pytest.raises(BrokerError, match="quantity=None"):
            gw.close_position(quantity=None)


def test_order_submission_blocked_by_safety_locks():
    """Default settings (dry_run=True) must keep order_submission_enabled False."""
    settings = make_settings()
    gw = _make_gateway_with_creds(settings)
    _prime_metadata(gw)
    assert gw.order_submission_enabled is False

    # Even enabling submission on the gateway isn't enough — dry_run kills it
    gw._allow_order_submission = True
    assert gw.order_submission_enabled is False


def test_all_safety_gates_required_for_live_orders():
    """All four gates must align for order_submission_enabled to be True."""
    settings = make_settings(
        dry_run=False,
        paper_trading=False,
        allow_live_trading=True,
        live_risk_acknowledgement="I_ACCEPT_LIVE_TRADING_RISK",
    )
    gw = _make_gateway_with_creds(settings)
    gw._allow_order_submission = True
    assert gw.order_submission_enabled is True

    # Removing any single gate → blocked
    gw._allow_order_submission = False
    assert gw.order_submission_enabled is False


def test_live_enabled_buy_routes_canonical_add_order_payload_without_network():
    """A fully enabled buy reaches AddOrder with a Kraken-ready payload.

    ``_private`` is mocked, so this proves routing without contacting Kraken or
    placing a real order.
    """
    settings = make_settings(
        dry_run=False,
        paper_trading=False,
        allow_live_trading=True,
        live_risk_acknowledgement="I_ACCEPT_LIVE_TRADING_RISK",
    )
    gw = _make_gateway_with_creds(settings)
    gw._allow_order_submission = True
    sized = MagicMock(
        pair="XXBTZUSD",
        volume_str="0.00050000",
        price_str="50000.0",
        notional=25.0,
    )

    def private_response(endpoint, _params):
        if endpoint == "AddOrder":
            return {"txid": ["TEST-ORDER-ID"]}
        assert endpoint == "QueryOrders"
        return {
            "TEST-ORDER-ID": {
                "status": "closed", "price": "50025.0", "vol_exec": "0.0005",
                "cost": "25.0125", "fee": "0.065",
            }
        }

    with (
        patch.object(gw, "size_buy", return_value=sized),
        patch.object(gw, "_private", side_effect=private_response) as private,
    ):
        order_id = gw.buy_notional(25.0, userref=42, signal_price=50000.0)

    assert order_id == "TEST-ORDER-ID"
    assert private.call_args_list[0].args == ("AddOrder", {
        "pair": "XXBTZUSD",
        "type": "buy",
        "ordertype": "market",
        "volume": "0.00050000",
        "userref": "42",
    })
    assert private.call_args_list[1].args == (
        "QueryOrders", {"txid": "TEST-ORDER-ID", "trades": True}
    )
    assert gw.last_fill is not None
    assert gw.last_fill.fill_price == 50025.0
    assert gw.last_fill.signal_price == 50000.0
    assert gw.last_fill.slippage_usd == 25.0
    assert gw.last_fill.slippage_bps == 5.0
    assert gw._executions.get("TEST-ORDER-ID") == gw.last_fill


def test_raw_add_order_strips_leverage_before_kraken_submission():
    settings = make_settings(
        dry_run=False,
        paper_trading=False,
        allow_live_trading=True,
        live_risk_acknowledgement="I_ACCEPT_LIVE_TRADING_RISK",
    )
    gw = _make_gateway_with_creds(settings)
    gw._allow_order_submission = True
    params = {
        "pair": "XXBTZUSD", "type": "buy", "ordertype": "limit",
        "price": "50000", "volume": "0.0005", "leverage": "2",
    }
    with patch.object(
        gw, "_private", return_value={"txid": ["SPOT-ONLY"]}
    ) as private:
        assert gw.add_order(params) == "SPOT-ONLY"
    submitted = private.call_args.args[1]
    assert "leverage" not in submitted

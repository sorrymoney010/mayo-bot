"""Tests for the Binance Spot gateway (broker-agnostic adapter).

Market-data tests hit the live Binance *public* REST API (no key required) so we
prove the adapter maps our canonical "BTC/USD" -> Binance "BTCUSDT" and parses
real candles.  Execution paths are exercised in dry-run (no key) so they record
an intent and return a synthetic id without touching the network.
"""

from __future__ import annotations

import pandas as pd
import pytest

from dublin_bot.binance_gateway import BinanceGateway
from dublin_bot.config import Settings


@pytest.fixture
def settings() -> Settings:
    # Live public data; no credentials -> keyless/dry path.
    return Settings(
        symbol="BTC/USD",
        timeframe_minutes=5,
        lookback_bars=220,
        binance_api_key="",
        binance_api_secret="",
    )


@pytest.fixture
def gw(settings: Settings) -> BinanceGateway:
    return BinanceGateway(settings, allow_order_submission=False)


def test_resolve_symbol_maps_usd_to_usdt(gw: BinanceGateway) -> None:
    meta = gw.resolve_symbol()
    assert meta.key == "BTCUSDT"
    assert meta.base == "BTC"
    assert meta.status == "online"
    assert meta.order_min > 0


def test_get_bars_returns_ohlcv(gw: BinanceGateway) -> None:
    bars = gw.get_bars(validate=False)
    assert isinstance(bars, pd.DataFrame) and len(bars) > 0
    for col in ("open", "high", "low", "close", "volume"):
        assert col in bars.columns
    assert bars.index.is_monotonic_increasing


def test_ticker(gw: BinanceGateway) -> None:
    t = gw.get_ticker_for("BTC/USD")
    assert t["last"] > 0
    assert t["bid"] <= t["last"] <= t["ask"] or t["ask"] >= t["bid"]


def test_freshness_pass_on_recent_bars(gw: BinanceGateway) -> None:
    bars = gw.get_bars(validate=False)
    verdict = gw.check_freshness(bars)
    assert verdict.fresh is True


def test_size_buy_returns_precision_sized_order(gw: BinanceGateway) -> None:
    from dublin_bot.precision import SizedOrder

    sized = gw.size_buy(100.0)
    assert isinstance(sized, SizedOrder)
    vol = float(sized.volume)
    assert vol > 0
    # volume_str must be a clean decimal string (no scientific notation).
    assert "e" not in sized.volume_str and "E" not in sized.volume_str


def test_buy_notional_dry_run_records_intent(gw: BinanceGateway) -> None:
    order_id = gw.buy_notional(50.0, userref=12345)
    assert order_id.startswith("binance-dry-buy-")
    assert not gw.order_submission_enabled


def test_close_position_dry_run(gw: BinanceGateway) -> None:
    assert gw.close_position(userref=999).startswith("binance-dry-sell-")


def test_account_equity_no_key_returns_budget(gw: BinanceGateway) -> None:
    # Without credentials we cannot read the account; fall back to budget.
    assert gw.account_equity() == float(gw.settings.strategy_equity_usd)


def test_health_reachable(gw: BinanceGateway) -> None:
    h = gw.health()
    assert h["broker"] == "binance"
    assert h["reachable"] is True

"""Every broker gateway must accept the exact calls the engine makes (names + keyword args)."""
from __future__ import annotations

import inspect

import pytest

from dublin_bot.binance_gateway import BinanceGateway
from dublin_bot.kraken_gateway import KrakenGateway

# Methods the engine invokes on self.gateway (see engine.py).
ENGINE_CALLS = ["buy_notional", "close_position", "add_order", "cancel_orders", "balances",
                "size_buy", "resolve_symbol", "get_bars", "get_ticker", "get_ticker_for",
                "has_position", "market_quality", "find_order_by_userref", "check_freshness",
                "account_equity"]


@pytest.mark.parametrize("name", ENGINE_CALLS)
def test_binance_matches_kraken_signature(name):
    assert hasattr(BinanceGateway, name), f"BinanceGateway missing {name}()"
    k = inspect.signature(getattr(KrakenGateway, name)).parameters
    b = inspect.signature(getattr(BinanceGateway, name)).parameters
    missing = [p for p in k if p not in b and k[p].kind is not inspect.Parameter.VAR_KEYWORD]
    assert not missing, f"BinanceGateway.{name} lacks params the engine may pass: {missing}"

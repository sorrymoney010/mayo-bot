"""
Multi-symbol market data adapter for the rotation strategy.

Wraps a KrakenGateway with per-call settings views to fetch multiple symbols.
The shared gateway settings are never mutated while another caller uses them.
"""

from __future__ import annotations

from copy import copy
from typing import Optional

from dublin_bot.kraken_gateway import KrakenGateway
from dublin_bot.config import Settings


class MultiSymbolGateway:
    """
    Wraps KrakenGateway to support multi-symbol bar fetching.
    
    Uses a symbol-scoped copy to fetch each coin without changing shared settings.
    """

    def __init__(self, gateway: KrakenGateway, settings: Settings):
        self.gateway = gateway
        self.settings = settings
        self._original_symbol = settings.symbol

    def for_symbol(self, symbol: str):
        """Bind one call without mutating shared gateway settings or metadata.

        The transport, nonce generator and rate limiter stay shared; symbol and
        last-fill state belong to this view. Do not use a view as ownership proof.
        """
        scoped = copy(self.gateway)
        scoped.settings = copy(self.gateway.settings)
        scoped.settings.symbol = symbol
        return scoped

    def get_bars_for(self, symbol: str) -> Optional[object]:
        """
        Fetch OHLC bars for a specific symbol.
        
        Args:
            symbol: Trading symbol like "PUMP/USD" or "BTC/USD"
        
        Returns:
            pandas DataFrame with OHLCV data, or None on failure
        """
        try:
            return self.for_symbol(symbol).get_bars(validate=False)
        except Exception as e:
            # Log but don't crash - just return None for this symbol
            print(f"Warning: Failed to fetch bars for {symbol}: {e}")
            return None

    def get_bars_for_all(self, symbols: list[str]) -> dict[str, Optional[object]]:
        """
        Fetch bars for all symbols in the list.
        
        Args:
            symbols: List of trading symbols
        
        Returns:
            Dict mapping symbol -> DataFrame or None
        """
        result = {}
        for symbol in symbols:
            bars = self.get_bars_for(symbol)
            result[symbol] = bars
        return result

    def get_ticker_for(self, symbol: str) -> Optional[dict]:
        """
        Fetch ticker data for a specific symbol.
        
        Args:
            symbol: Trading symbol
        
        Returns:
            Ticker dict with bid/ask/last/volume, or None on failure
        """
        try:
            return self.for_symbol(symbol).get_ticker()
        except Exception as e:
            print(f"Warning: Failed to fetch ticker for {symbol}: {e}")
            return None

    def get_current_prices(self, symbols: list[str]) -> dict[str, float]:
        """
        Get current prices for all symbols.
        
        Args:
            symbols: List of trading symbols
        
        Returns:
            Dict mapping symbol -> current price (float)
        """
        prices = {}
        for symbol in symbols:
            ticker = self.get_ticker_for(symbol)
            if ticker and "last" in ticker:
                prices[symbol] = float(ticker["last"])
            else:
                prices[symbol] = 0.0
        return prices

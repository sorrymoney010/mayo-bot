"""Kraken Futures PUBLIC market data. No keys, no order routes.

* REST tickers (last, mark, funding rate, open interest)
* historical funding rates
* public trade candles

The live order path lives in ``venue_locks.submit_futures_order`` and is not
imported here.
"""
from __future__ import annotations

import json
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from .futures_costs import relative_funding_rate

TICKERS_URL = "https://futures.kraken.com/derivatives/api/v3/tickers"
FUNDING_URL = "https://futures.kraken.com/derivatives/api/v3/historical-funding-rates"
CHARTS_URL = "https://futures.kraken.com/api/charts/v1/trade/{symbol}/{resolution}"
INSTRUMENTS_URL = "https://futures.kraken.com/derivatives/api/v3/instruments"


class FuturesPublicError(RuntimeError):
    pass


class FuturesPublic:
    """Read-only client. ``opener`` is injectable so tests never touch the network."""

    def __init__(self, *, timeout: float = 15.0, opener=None) -> None:
        self.timeout = timeout
        self._opener = opener or self._urlopen

    @staticmethod
    def _urlopen(url: str, timeout: float) -> bytes:
        req = Request(url, headers={"User-Agent": "mayo-bot-paper/1.0"})
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 — public GET, URL is fixed
            return resp.read()

    def _get(self, url: str) -> Any:
        try:
            raw = self._opener(url, self.timeout)
        except (URLError, TimeoutError, OSError) as exc:
            raise FuturesPublicError(str(exc)) from exc
        if isinstance(raw, str):
            raw = raw.encode()
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise FuturesPublicError("futures public payload was not JSON") from exc

    def tickers(self) -> list[dict]:
        payload = self._get(TICKERS_URL)
        rows = payload.get("tickers") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise FuturesPublicError("tickers payload missing 'tickers'")
        return [r for r in rows if isinstance(r, dict)]

    def marks(self, symbols: list[str]) -> dict[str, float]:
        """Mark (or last) for each symbol from a single public tickers download."""
        wanted = {s.upper() for s in symbols}
        out: dict[str, float] = {}
        for row in self.tickers():
            sym = str(row.get("symbol", "")).upper()
            if sym not in wanted:
                continue
            raw = row.get("markPrice")
            if raw is None:
                raw = row.get("last")
            try:
                px = float(raw)
            except (TypeError, ValueError):
                continue
            if px > 0:
                out[sym] = px
        return out

    def ticker(self, symbol: str) -> dict:
        sym = symbol.upper()
        for row in self.tickers():
            if str(row.get("symbol", "")).upper() == sym:
                return row
        raise FuturesPublicError(f"no public ticker for {sym}")

    def snapshot(self, symbol: str) -> dict:
        """Normalised public quote. Prices are floats; missing fields stay None."""
        row = self.ticker(symbol)
        def _f(key: str) -> float | None:
            try:
                val = row.get(key)
                return None if val is None else float(val)
            except (TypeError, ValueError):
                return None
        last = _f("last")
        mark = _f("markPrice")
        if mark is None:
            mark = last
        absolute = _f("fundingRate")
        return {
            "symbol": symbol.upper(),
            "last": last,
            "mark": mark,
            "bid": _f("bid"),
            "ask": _f("ask"),
            "funding_rate": absolute,
            "funding_rate_relative": relative_funding_rate(absolute, mark),
            "funding_prediction": _f("fundingRatePrediction"),
            "open_interest": _f("openInterest"),
        }

    def funding_history(self, symbol: str) -> list[dict]:
        url = f"{FUNDING_URL}?symbol={symbol.upper()}"
        payload = self._get(url)
        rows = payload.get("rates") if isinstance(payload, dict) else None
        if rows is None and isinstance(payload, dict):
            rows = payload.get("fundingRates") or payload.get("history")
        if not isinstance(rows, list):
            raise FuturesPublicError("funding history payload had no rates")
        return [r for r in rows if isinstance(r, dict)]

    def candles(self, symbol: str, resolution: str = "1h") -> list[dict]:
        url = CHARTS_URL.format(symbol=symbol.upper(), resolution=resolution)
        payload = self._get(url)
        rows = payload.get("candles") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise FuturesPublicError("charts payload missing candles")
        return [r for r in rows if isinstance(r, dict)]

    def maintenance_margin(self, symbol: str, *, default: float) -> float:
        """Smallest published maintenance margin for ``symbol``, else ``default``."""
        try:
            payload = self._get(INSTRUMENTS_URL)
        except FuturesPublicError:
            return default
        rows = payload.get("instruments") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return default
        sym = symbol.upper()
        for inst in rows:
            if str(inst.get("symbol", "")).upper() != sym:
                continue
            levels = inst.get("marginLevels") or inst.get("marginSchedules") or []
            vals = []
            for lvl in levels if isinstance(levels, list) else []:
                for key in ("maintenanceMargin", "maintenance_margin", "mm"):
                    if key in lvl:
                        try:
                            vals.append(float(lvl[key]))
                        except (TypeError, ValueError):
                            pass
            if vals:
                return min(vals)
        return default

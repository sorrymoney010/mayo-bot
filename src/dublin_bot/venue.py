"""Venue-neutral PUBLIC market data for helpers that have no gateway handy.

The daily D1 filter, shadow scoring, loop telemetry, the exit watcher and
research tools only need public candles and last prices. They used to call
Kraken directly; they now ask this module, which follows ``BROKER``:

* ``kraken``  -> Kraken public REST (unchanged behaviour)
* ``binance`` -> Binance public REST on ``BINANCE_HOST`` (default Binance.US)

Never touches a private endpoint and never places an order.
"""

from __future__ import annotations

import os
import threading
import time
from functools import lru_cache

_KRAKEN_PAIR = {"BTC/USD": "XBTUSD", "ETH/USD": "ETHUSD", "SOL/USD": "SOLUSD"}
_lock = threading.Lock()
_binance = None


@lru_cache(maxsize=1)
def _settings_broker() -> str:
    try:
        from .config import Settings
        return str(Settings().broker)
    except Exception:
        return "kraken"


def active_broker() -> str:
    """``BROKER`` from the environment, else from ``.env`` via Settings."""
    env = os.environ.get("BROKER")
    if env:
        raw = env.strip().lower().replace(".", "").replace("-", "").replace("_", "")
        return "binance" if raw in ("binance", "binanceus") else env.strip().lower()
    return _settings_broker()


def is_binance() -> bool:
    return active_broker() == "binance"


def binance_public():
    """Shared keyless Binance gateway (public endpoints only)."""
    global _binance
    with _lock:
        if _binance is None:
            from .binance_gateway import BinanceGateway
            from .config import Settings
            try:
                base = Settings()
            except Exception:
                base = Settings(_env_file=None)
            # Keyless + paper: the gateway physically cannot sign a request.
            settings = base.model_copy(update={
                "broker": "binance", "binance_api_key": "", "binance_api_secret": "",
                "paper_trading": True, "dry_run": True, "allow_live_trading": False,
            })
            _binance = BinanceGateway(settings, allow_order_submission=False, max_retries=2)
        return _binance


def reset_cache() -> None:
    """Testing hook: forget the cached gateway and broker."""
    global _binance
    with _lock:
        _binance = None
    _settings_broker.cache_clear()


def public_ohlc(symbol: str, tf_minutes: int, *, retries: int = 2) -> list[list]:
    """Kraken-shaped OHLC rows ``[open_time_s, o, h, l, c, vwap, volume]``.

    Like Kraken, the final row may be the still-forming candle; callers drop
    bars whose ``time + tf`` is in the future.
    """
    if is_binance():
        gw = binance_public()
        out = []
        for r in gw.klines(symbol, int(tf_minutes), limit=1000):
            vol = float(r[5])
            vwap = float(r[7]) / vol if vol > 0 else float(r[4])
            out.append([int(r[0]) // 1000, r[1], r[2], r[3], r[4], str(vwap), r[5], int(r[8])])
        return out
    from .pipeline.kraken_rest import KrakenPublic
    pair = _KRAKEN_PAIR.get(symbol.upper(), symbol.replace("/", ""))
    return KrakenPublic(min_interval=0.0, timeout=10.0).ohlc(pair, int(tf_minutes), retries=retries)


def public_prices(symbols: list[str]) -> dict[str, float]:
    """Last traded price per canonical symbol (missing symbols are omitted)."""
    if not symbols:
        return {}
    if is_binance():
        gw = binance_public()
        prices = gw.last_prices()
        out: dict[str, float] = {}
        for sym in symbols:
            try:
                pair = gw._pair_for(sym)
            except Exception:
                continue
            if pair in prices:
                out[sym] = prices[pair]
        return out
    from .pipeline.kraken_rest import KrakenPublic
    base_of = {"BTC/USD": "XBT", "ETH/USD": "ETH", "SOL/USD": "SOL"}
    pairs = ",".join(base_of.get(s, s.split("/")[0]) + "USD" for s in symbols)
    res = KrakenPublic(min_interval=0.0, timeout=10.0).get("Ticker", {"pair": pairs}, retries=2)
    out = {}
    for sym in symbols:
        base = base_of.get(sym, sym.split("/")[0])
        for k, v in res.items():
            if base in k and k.endswith("USD"):
                out[sym] = float(v["c"][0])
                break
    return out


def public_last(symbol: str) -> float | None:
    """One last price; None on any failure."""
    try:
        return public_prices([symbol]).get(symbol)
    except Exception:
        return None


def history(symbol: str, days: int, interval_min: int = 60, *, sleep_s: float = 0.2):
    """Paged closed OHLC history (DataFrame time/open/high/low/close/volume) from Binance."""
    import pandas as pd

    gw = binance_public()
    want_since_ms = int((time.time() - days * 86400) * 1000)
    rows: list[list] = []
    end_ms = None
    for _ in range(200):
        page = gw.klines(symbol, int(interval_min), limit=1000, end_ms=end_ms)
        if not page:
            break
        rows = page + rows
        if int(page[0][0]) <= want_since_ms or len(page) < 1000:
            break
        end_ms = int(page[0][0]) - 1
        time.sleep(sleep_s)
    now_ms = int(time.time() * 1000)
    data = {int(r[0]): r for r in rows if int(r[6]) < now_ms and int(r[0]) >= want_since_ms}
    df = pd.DataFrame(
        [{"time": k // 1000, "open": float(r[1]), "high": float(r[2]), "low": float(r[3]),
          "close": float(r[4]), "volume": float(r[5])} for k, r in sorted(data.items())],
        columns=["time", "open", "high", "low", "close", "volume"],
    )
    return df.reset_index(drop=True)

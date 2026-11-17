"""Venue OHLC fetch (Kraken or Binance.US) with local cache. Research-only. No orders."""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd

from dublin_bot.kraken_gateway import KrakenGateway  # noqa: F401  (type of `gateway`)

CACHE_DIR = Path("logs")


def cache_path(symbol: str, interval_min: int, broker: str = "kraken") -> Path:
    safe = symbol.replace("/", "-").upper()
    venue = "" if str(broker).lower() == "kraken" else f"_{str(broker).lower()}"
    return CACHE_DIR / f"ohlc_{safe}_{interval_min}m{venue}.csv"


def _parse_ohlc(raw: dict) -> pd.DataFrame:
    pair_key = next(k for k in raw if k != "last")
    rows = raw[pair_key]
    if not rows:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    cols = ["time", "open", "high", "low", "close", "vwap", "volume", "count"]
    for ci, name in enumerate(cols):
        if ci < df.shape[1]:
            df[name] = pd.to_numeric(df[ci], errors="coerce")
    df = df[["time", "open", "high", "low", "close", "volume"]].dropna()
    df = df[df["close"] > 0].drop_duplicates("time").sort_values("time")
    return df.reset_index(drop=True)


def fetch_history_paged(
    gateway: KrakenGateway,
    symbol: str,
    days: int,
    interval_min: int = 60,
    sleep_s: float = 0.35,
) -> pd.DataFrame:
    """Page public OHLC of the configured venue (Kraken ~720 candles/call; Binance 1000)."""
    if str(getattr(gateway.settings, "broker", "kraken")).lower() == "binance":
        from .venue import history
        return history(symbol, days, interval_min)
    gw = KrakenGateway(gateway.settings)
    gw.settings.symbol = symbol
    meta = gw.resolve_symbol()
    want_since = int(time.time()) - days * 86400
    since = want_since
    chunks: list[pd.DataFrame] = []
    last_max = -1.0
    for _ in range(40):
        raw = gw._public(
            "OHLC",
            {"pair": meta.key, "interval": interval_min, "since": str(since)},
        )
        part = _parse_ohlc(raw)
        if part.empty:
            break
        tmax = float(part["time"].max())
        if tmax <= last_max:
            break
        chunks.append(part)
        last_max = tmax
        since = int(tmax)
        if tmax >= time.time() - interval_min * 60:
            break
        time.sleep(sleep_s)
    if not chunks:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    df = pd.concat(chunks, ignore_index=True)
    df = df.drop_duplicates("time").sort_values("time")
    df = df[df["time"] >= want_since]
    if len(df) > 1:
        df = df.iloc[:-1]
    return df.reset_index(drop=True)


def load_or_fetch(
    gateway: KrakenGateway,
    symbol: str,
    days: int,
    interval_min: int = 60,
    cache: Path | None = None,
    refresh: bool = False,
) -> pd.DataFrame:
    path = cache or cache_path(symbol, interval_min, getattr(gateway.settings, "broker", "kraken"))
    path.parent.mkdir(parents=True, exist_ok=True)
    cached = pd.DataFrame()
    if path.exists() and not refresh:
        cached = pd.read_csv(path)
        if "time" in cached.columns:
            cached = cached.dropna(subset=["time"])
    live = fetch_history_paged(gateway, symbol, days, interval_min)
    if cached.empty:
        out = live
    elif live.empty:
        out = cached
    else:
        out = pd.concat([cached, live], ignore_index=True)
        out = out.drop_duplicates("time").sort_values("time").reset_index(drop=True)
    if not out.empty:
        out.to_csv(path, index=False)
    return out

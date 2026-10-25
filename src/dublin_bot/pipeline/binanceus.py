"""Binance.US PUBLIC market data (no keys, read-only): REST aggTrades/klines + WebSocket aggTrade/bookTicker.

Plugs into the existing pipeline unchanged: ``BinanceUSPublic.trades`` has the same contract as
``KrakenPublic.trades`` (so ``backfill_range``/``verify_hole`` work), and ``BinanceUSCollector`` is a
``TickCollector`` that only swaps the socket protocol. Ticks land in ``<data>/binanceus/ticks/<SYM>/`` with the
same file layout/_status.json, keyed by Binance's per-symbol sequential aggregate-trade id. Canonical symbols
stay BTC/USD, ETH/USD, SOL/USD (mapped to the more liquid USDT books; checked 2026-10-25: USDT 24h quote volume
~1.5-2x the USD books). Nothing here can place, cancel or modify orders.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Callable

import pandas as pd
import requests

from . import PAIRS, canonical
from .collector import TickCollector
from .tickstore import Tick

API = "https://api.binance.us/api/v3/"
WS_BASE = "wss://stream.binance.us/stream?streams="  # port 443; the box cannot reach :9443
SYMBOL_MAP = {"BTC/USD": "BTCUSDT", "ETH/USD": "ETHUSDT", "SOL/USD": "SOLUSDT"}
VENUES = ("kraken", "binanceus")
FEES_BPS = {"binanceus": (0.0, 2.0), "kraken": (40.0, 80.0)}  # (maker, taker); Binance.US zero-fee schedule


def venue() -> str:
    v = os.environ.get("MARKET_VENUE", "binanceus").strip().lower()
    return v if v in VENUES else "binanceus"


def venue_data_dir(data_dir: Path | str, v: str | None = None) -> Path:
    v = v or venue()
    return Path(data_dir) if v == "kraken" else Path(data_dir) / v


def bsym(symbol: str) -> str:
    c = canonical(symbol)
    if c is None:  # Kraken REST pair spelling (XBTUSD) also canonicalises
        raise ValueError(symbol)
    return SYMBOL_MAP[c]


class BinanceUSPublic:
    def __init__(self, *, session: requests.Session | None = None, timeout: float = 20.0,
                 min_interval: float = 0.25, sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] | None = None, now_fn: Callable[[], float] = time.time) -> None:
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "mayo-bot-pipeline/1.0")
        self.timeout, self.min_interval, self._sleep, self.now = timeout, min_interval, sleep, now_fn
        self._last = 0.0
        self._log = log or (lambda m: print(m, file=sys.stderr, flush=True))

    def get(self, path: str, params: dict, *, retries: int = 6):
        delay = 2.0
        for attempt in range(retries):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.session.get(API + path, params=params, timeout=self.timeout)
                if r.status_code in (418, 429) or r.status_code >= 500:
                    raise requests.RequestException(f"http {r.status_code}")
                if r.status_code in (403, 451):
                    raise RuntimeError(f"Binance.US {path}: http {r.status_code} (geo/permission block)")
                r.raise_for_status()
                return r.json()
            except requests.RequestException as exc:
                if attempt == retries - 1:
                    raise
                self._log(f"binanceus REST {path} retry after {exc}")
                self._sleep(delay)
                delay = min(delay * 2, 60)
        raise RuntimeError(f"Binance.US {path} failed")

    @staticmethod
    def _tick(r: dict) -> Tick:
        # m=True: buyer is maker -> the aggressor sold
        return Tick(trade_id=int(r["a"]), ts=int(r["T"]) / 1000.0, price=str(r["p"]), qty=str(r["q"]),
                    side="s" if r.get("m") else "b", ord_type="m")

    def trades(self, pair: str, since: str | int | None, count: int = 1000) -> tuple[list[Tick], str]:
        """One page of aggTrades. ``since`` = unix seconds, or ``id:<n>`` cursor from a previous page."""
        s = bsym(pair)
        if since is not None and str(since).startswith("id:"):
            rows = self.get("aggTrades", {"symbol": s, "fromId": int(str(since)[3:]), "limit": count})
        else:
            start = int(float(since if since is not None else self.now() - 3600) * 1000)
            rows = []
            while not rows and start < self.now() * 1000:  # aggTrades time windows are <= 1h
                rows = self.get("aggTrades", {"symbol": s, "startTime": start,
                                              "endTime": start + 3_599_999, "limit": count})
                start += 3_600_000
        ticks = [self._tick(r) for r in rows]
        return ticks, (f"id:{ticks[-1].trade_id + 1}" if ticks else str(since or ""))

    def klines(self, symbol: str, tf_minutes: int, limit: int = 720) -> pd.DataFrame:
        iv = {1: "1m", 15: "15m", 30: "30m", 60: "1h", 240: "4h"}[int(tf_minutes)]
        rows = self.get("klines", {"symbol": bsym(symbol), "interval": iv, "limit": limit})
        df = pd.DataFrame([r[:6] for r in rows], columns=["t", "open", "high", "low", "close", "volume"])
        df.index = pd.to_datetime(df.t.astype("int64"), unit="ms", utc=True)
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        now = self.now()
        return df[df.index.map(lambda x: x.timestamp() + tf_minutes * 60 <= now)]  # closed bars only


class BinanceUSCollector(TickCollector):
    """TickCollector over Binance.US combined streams (``<sym>@aggTrade`` + ``<sym>@bookTicker``)."""

    def __init__(self, store, **kw) -> None:
        kw.setdefault("client", BinanceUSPublic(log=kw.get("log", print)))
        super().__init__(store, **kw)
        streams = "/".join(f"{SYMBOL_MAP[s].lower()}@aggTrade/{SYMBOL_MAP[s].lower()}@bookTicker"
                           for s in self.symbols)
        self.ws_url = WS_BASE + streams
        self._rev = {v: k for k, v in SYMBOL_MAP.items()}

    def subscribe_messages(self) -> list[str]:
        return []  # streams are in the URL

    def handle(self, raw) -> None:
        msg = json.loads(raw)
        if not isinstance(msg, dict):
            return
        self.last_msg = self.now()
        d = msg.get("data", msg)
        sym = self._rev.get(str(d.get("s", "")).upper())
        if sym not in self.st:
            return
        if d.get("e") == "aggTrade":
            try:
                self.on_trade(sym, BinanceUSPublic._tick(d))
            except (KeyError, TypeError, ValueError):
                return
        elif "b" in d and "a" in d:  # bookTicker
            s = self.st[sym]
            now = self.now()
            try:
                bid, ask = float(d["b"]), float(d["a"])
            except (TypeError, ValueError):
                return
            if s["last_id"] is not None:
                s["in_sync"] = True  # socket confirmed live after REST catch-up (thin books trade rarely)
            if now - s["last_quote"] >= self.quote_every and bid > 0 and ask > 0:
                s["quotes"].append((now, bid, ask))
                s["last_quote"] = now


assert set(SYMBOL_MAP) == set(PAIRS)

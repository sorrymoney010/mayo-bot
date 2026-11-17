"""Daily risk-on filter (D1) for ENTRIES.

    riskon(day D) = close_D > SMA50_D  and  SMA50_D > SMA50_{D-5}

computed on CLOSED UTC daily bars only. Day D's value becomes usable at
D+1 00:00 UTC (when bar D has closed). It gates new entries only — it never
forces an exit. Missing/short data => unknown => entries blocked (fail closed).

Sources (first that works):
  1. Kraken PUBLIC REST OHLC interval=1440 (~720 days; no keys, never private).
  2. ``fetch`` override (tests / backtests pass their own daily frame).
Values are cached per symbol and refreshed once the UTC day rolls over (or
after ``ttl`` seconds), so the 5-minute loop does not re-download.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Callable

import numpy as np
import pandas as pd

DAY = 86400
SMA_N = 50
RISE_N = 5


def riskon_table(daily: pd.DataFrame, *, now: float | None = None) -> pd.DataFrame:
    """daily: columns time (bar open, epoch s) + close. Returns one row per
    CLOSED day with close_time (= open + 1 day), close, sma50, sma50_prev,
    riskon (1.0 / 0.0 / NaN while SMA50 is warming up)."""
    if daily is None or not len(daily):
        return pd.DataFrame(columns=["close_time", "close", "sma50", "sma50_prev", "riskon"])
    k = daily[["time", "close"]].copy()
    k["time"] = pd.to_numeric(k["time"], errors="coerce")
    k["close"] = pd.to_numeric(k["close"], errors="coerce")
    k = k.dropna().drop_duplicates("time").sort_values("time")
    now = time.time() if now is None else float(now)
    k = k[k["time"] + DAY <= now]  # closed days only
    sma = k["close"].rolling(SMA_N).mean()
    prev = sma.shift(RISE_N)
    on = np.where(sma.isna() | prev.isna(), np.nan, ((k["close"] > sma) & (sma > prev)).astype(float))
    return pd.DataFrame({"close_time": (k["time"] + DAY).astype(np.int64).to_numpy(),
                         "close": k["close"].to_numpy(float), "sma50": sma.to_numpy(float),
                         "sma50_prev": prev.to_numpy(float), "riskon": on})


def attach_d1(d: pd.DataFrame, table: pd.DataFrame, tf_minutes: int) -> pd.DataFrame:
    """Add ``d1_riskon`` / ``d1_sma50`` to an intraday frame (``time`` = bar open).

    The value for an intraday bar is the latest daily row whose close_time is
    at or before that bar's CLOSE (no look-ahead)."""
    d = d.copy()
    opens = bar_open_times(d)
    if table is None or not len(table) or opens is None:
        d["d1_riskon"] = np.nan
        d["d1_sma50"] = np.nan
        return d
    bar_close = opens + tf_minutes * 60
    ct = table["close_time"].to_numpy(float)
    idx = np.searchsorted(ct, bar_close, side="right") - 1
    ok = idx >= 0
    on = np.full(len(d), np.nan)
    sma = np.full(len(d), np.nan)
    on[ok] = table["riskon"].to_numpy(float)[idx[ok]]
    sma[ok] = table["sma50"].to_numpy(float)[idx[ok]]
    d["d1_riskon"] = on
    d["d1_sma50"] = sma
    return d


def bar_open_times(d: pd.DataFrame) -> np.ndarray | None:
    """Bar-open epoch seconds from a ``time`` column or a DatetimeIndex."""
    if "time" in d:
        return pd.to_numeric(d["time"], errors="coerce").to_numpy(float)
    if isinstance(d.index, pd.DatetimeIndex):
        idx = d.index if d.index.tz is not None else d.index.tz_localize("UTC")
        return (idx.asi8 // 10**9).astype(float)
    return None


def d1_mask(d: pd.DataFrame) -> np.ndarray:
    """Entry mask: True only where the D1 filter is known AND on (fail closed)."""
    if "d1_riskon" not in d:
        return np.zeros(len(d), dtype=bool)
    return np.nan_to_num(d["d1_riskon"].to_numpy(float), nan=0.0) > 0.5


# ── live provider (public REST, cached) ──────────────────────────
_PAIR = {"BTC/USD": "XBTUSD", "ETH/USD": "ETHUSD", "SOL/USD": "SOLUSD"}


def _rest_daily(symbol: str) -> pd.DataFrame:
    from .venue import public_ohlc
    # Active venue (BROKER): Kraken or Binance.US public daily candles.
    # few retries: an exchange hiccup must not stall the paper loop (stale same-day cache / fail closed)
    rows = public_ohlc(symbol, 1440, retries=2)
    return pd.DataFrame({"time": [int(r[0]) for r in rows], "close": [float(r[4]) for r in rows]})


class DailyFilter:
    def __init__(self, fetch: Callable[[str], pd.DataFrame] | None = None, *, ttl: float = 3600.0,
                 now_fn: Callable[[], float] = time.time) -> None:
        self.fetch = fetch or _rest_daily
        self.ttl = ttl
        self.now_fn = now_fn
        self._cache: dict[str, tuple[float, str, pd.DataFrame]] = {}
        self._lock = threading.Lock()

    def table(self, symbol: str) -> pd.DataFrame | None:
        now = float(self.now_fn())
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        sym = symbol.upper()
        with self._lock:
            hit = self._cache.get(sym)
            if hit and hit[1] == day and now - hit[0] < self.ttl:
                return hit[2]
        try:
            tab = riskon_table(self.fetch(sym), now=now)
        except Exception:
            return hit[2] if hit and hit[1] == day else None  # stale-same-day ok, else fail closed
        with self._lock:
            self._cache[sym] = (now, day, tab)
        return tab

    def state(self, symbol: str) -> dict:
        tab = self.table(symbol)
        if tab is None or not len(tab):
            return {"riskon": None, "reason": "daily data unavailable (fail closed)"}
        last = tab.iloc[-1]
        on = last["riskon"]
        known = isinstance(on, float) and math.isfinite(on)
        return {"riskon": (bool(on > 0.5) if known else None),
                "day": time.strftime("%Y-%m-%d", time.gmtime(int(last["close_time"]) - DAY)),
                "close": round(float(last["close"]), 8), "sma50": _r(last["sma50"]),
                "sma50_prev5": _r(last["sma50_prev"]),
                "reason": "ok" if known else "SMA50 warming up (fail closed)"}

    def attach(self, d: pd.DataFrame, symbol: str, tf_minutes: int) -> pd.DataFrame:
        return attach_d1(d, self.table(symbol), tf_minutes)


def _r(x) -> float | None:
    try:
        x = float(x)
        return round(x, 8) if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def live_d1(strategy, d: pd.DataFrame, tf_minutes: int) -> pd.DataFrame:
    """Attach D1 for the strategy's current symbol (live/paper strategies)."""
    flt = getattr(strategy, "daily_filter", None) or default_filter()
    return flt.attach(d, str(strategy.settings.symbol), tf_minutes)


def d1_reason(d: pd.DataFrame, i: int = -1) -> str:
    if "d1_riskon" not in d:
        return "D1 off"
    v = d["d1_riskon"].iloc[i]
    sma = d["d1_sma50"].iloc[i] if "d1_sma50" in d else float("nan")
    if not (isinstance(v, float) and math.isfinite(v)):
        return "D1 unknown (daily data unavailable/warming up) — entries blocked"
    return f"D1 {'risk-on' if v > 0.5 else 'risk-off'} (sma50_d={sma:.6g})"


_DEFAULT: DailyFilter | None = None


def default_filter() -> DailyFilter:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = DailyFilter()
    return _DEFAULT

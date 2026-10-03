"""Ticks -> 1m bars (+ microstructure) -> 15m / 1h / 4h bars.

A bar is emitted ONLY when the tick stream provably covers its whole
interval, i.e. it lies inside one *coverage run*: a stretch of stored trades
with consecutive Kraken trade ids (so no trade can be missing) whose first
trade is before the bar opens and whose end (last trade, or the collector's
``live_through`` heartbeat for the newest run) is at/after the bar closes.
Partial bars, bars over an id hole and the still-forming bar are never
emitted, so callers can safely fall back to Kraken REST OHLC for them.

Per closed UTC day, 1m bars and a coverage summary are cached under
``<root>/bars1m/<SYM>/<day>.csv.gz`` + ``.json`` (rebuilt when the source
tick/quote files change); the open day is always rebuilt from ticks.

Bar columns: open high low close vwap volume trades buy_vol sell_vol
buy_count sell_count ofi spread_bps (index: UTC bar-open ``timestamp``).
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from . import fs_key
from .tickstore import TickStore, _day_bounds, utc_day

BAR_COLS = ["open", "high", "low", "close", "vwap", "volume", "trades",
            "buy_vol", "sell_vol", "buy_count", "sell_count", "ofi", "spread_bps"]
_SUM_COLS = ["volume", "trades", "buy_vol", "sell_vol", "buy_count", "sell_count"]
CACHE_VERSION = 1


@dataclass
class DaySummary:
    day: str
    n: int
    first_id: int | None
    first_ts: float | None
    last_id: int | None
    last_ts: float | None
    holes: list[tuple[float, float]]  # (ts_before, ts_after) for each id gap

    def to_dict(self) -> dict:
        return self.__dict__ | {"holes": [list(h) for h in self.holes]}


def ticks_to_1m(ticks: pd.DataFrame, quotes: pd.DataFrame | None = None) -> pd.DataFrame:
    """Aggregate ticks into 1-minute bars (only minutes that had trades)."""
    if ticks is None or not len(ticks):
        return _empty_bars()
    t = ticks.sort_values("trade_id", kind="stable")
    minute = (t["ts"].to_numpy(float) // 60).astype(np.int64) * 60
    price = t["price"].to_numpy(float)
    qty = t["qty"].to_numpy(float)
    buy = (t["side"].to_numpy() == "b")
    df = pd.DataFrame({"m": minute, "price": price, "qty": qty, "pq": price * qty,
                       "bq": np.where(buy, qty, 0.0), "sq": np.where(buy, 0.0, qty),
                       "bc": buy.astype(np.int64), "sc": (~buy).astype(np.int64)})
    g = df.groupby("m", sort=True)
    out = pd.DataFrame({
        "open": g["price"].first(), "high": g["price"].max(), "low": g["price"].min(),
        "close": g["price"].last(), "volume": g["qty"].sum(), "pq": g["pq"].sum(),
        "trades": g["price"].size(), "buy_vol": g["bq"].sum(), "sell_vol": g["sq"].sum(),
        "buy_count": g["bc"].sum(), "sell_count": g["sc"].sum(),
    })
    out["vwap"] = out["pq"] / out["volume"].replace(0, np.nan)
    out = out.drop(columns="pq")
    out["ofi"] = (out["buy_vol"] - out["sell_vol"]) / out["volume"].replace(0, np.nan)
    out["spread_bps"] = np.nan
    if quotes is not None and len(quotes):
        q = quotes
        mid = (q["bid"] + q["ask"]) / 2
        sp = ((q["ask"] - q["bid"]) / mid * 1e4).where(mid > 0)
        qm = (q["ts"].to_numpy(float) // 60).astype(np.int64) * 60
        spm = pd.Series(sp.to_numpy(float), index=qm).groupby(level=0).mean()
        out["spread_bps"] = spm.reindex(out.index)
    out.index = pd.DatetimeIndex(pd.to_datetime(out.index, unit="s", utc=True), name="timestamp")
    return out[BAR_COLS]


def summarize(ticks: pd.DataFrame, day: str,
              exchange_holes: set[tuple[int, int]] | None = None) -> DaySummary:
    """Coverage summary; id holes Kraken itself never published are not holes."""
    if ticks is None or not len(ticks):
        return DaySummary(day, 0, None, None, None, None, [])
    ids = ticks["trade_id"].to_numpy(np.int64)
    ts = ticks["ts"].to_numpy(float)
    holes = [i for i in np.nonzero(np.diff(ids) > 1)[0]
             if not exchange_holes or (int(ids[i] + 1), int(ids[i + 1] - 1)) not in exchange_holes]
    return DaySummary(day, int(len(ids)), int(ids[0]), float(ts[0]), int(ids[-1]), float(ts[-1]),
                      [(float(ts[i]), float(ts[i + 1])) for i in holes])


def coverage_runs(days: list[DaySummary], *, live_through: float | None = None,
                  live_last_id: int | None = None,
                  exchange_holes: set[tuple[int, int]] | None = None) -> list[tuple[float, float]]:
    """Merge per-day summaries into [(start_ts, end_ts)] runs of id-continuous ticks."""
    runs: list[list[float]] = []
    prev_last_id: int | None = None
    for s in sorted((d for d in days if d.n), key=lambda d: d.first_id):
        if runs and prev_last_id is not None and (
                s.first_id == prev_last_id + 1
                or (exchange_holes and (prev_last_id + 1, s.first_id - 1) in exchange_holes)):
            pass  # continues the previous run across the day boundary
        else:
            runs.append([s.first_ts, s.first_ts])
        for before, after in s.holes:
            runs[-1][1] = before
            runs.append([after, after])
        runs[-1][1] = s.last_ts
        prev_last_id = s.last_id
    if (runs and live_through is not None and live_last_id is not None
            and prev_last_id is not None and live_last_id <= prev_last_id
            and live_through > runs[-1][1]):
        runs[-1][1] = float(live_through)
    return [(a, b) for a, b in runs]


def resample(m1: pd.DataFrame, tf_minutes: int, runs: list[tuple[float, float]],
             *, now: float | None = None) -> pd.DataFrame:
    """Aggregate 1m bars to ``tf_minutes`` keeping only fully-covered closed bars."""
    if not runs:
        return _empty_bars()
    tfs = tf_minutes * 60
    now = time.time() if now is None else now
    # Bucket [s, s+tf) is covered by run (a, b) iff a < s and s + tf <= min(b, now).
    starts: list[np.ndarray] = []
    for a, b in runs:
        lo = math.floor(a / tfs) * tfs + tfs          # first boundary strictly after a
        hi = math.floor(min(b, now) / tfs) * tfs      # exclusive: last bucket ends <= b
        if hi > lo:
            starts.append(np.arange(lo, hi, tfs, dtype=np.int64))
    if not starts:
        return _empty_bars()
    buckets = np.unique(np.concatenate(starts))
    if not len(buckets):
        return _empty_bars()
    src = m1 if m1 is not None and len(m1) else _empty_bars()
    sec = (src.index.asi8 // 10**9).astype(np.int64)
    key = (sec // tfs) * tfs
    keep = np.isin(key, buckets)
    s = src[keep].copy()
    s["_k"] = key[keep]
    s["_pq"] = s["vwap"].fillna(s["close"]) * s["volume"]
    s["_spw"] = s["spread_bps"]
    g = s.groupby("_k", sort=True)
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                        "close": g["close"].last(), "_pq": g["_pq"].sum(),
                        "spread_bps": g["_spw"].mean()})
    for c in _SUM_COLS:
        out[c] = g[c].sum()
    out = out.reindex(buckets)
    # Covered buckets with no trades at all: flat bar at the previous close.
    empty = out["open"].isna()
    if empty.any():
        prev_close = out["close"].ffill()
        for c in ("open", "high", "low", "close"):
            out.loc[empty, c] = prev_close[empty]
        for c in _SUM_COLS + ["_pq"]:
            out.loc[empty, c] = 0.0
        out = out.dropna(subset=["close"])
    out["vwap"] = out["_pq"] / out["volume"].replace(0, np.nan)
    out["ofi"] = (out["buy_vol"] - out["sell_vol"]) / out["volume"].replace(0, np.nan)
    out = out.drop(columns="_pq")
    out["trades"] = out["trades"].astype(np.int64)
    out["buy_count"] = out["buy_count"].astype(np.int64)
    out["sell_count"] = out["sell_count"].astype(np.int64)
    out.index = pd.DatetimeIndex(pd.to_datetime(out.index, unit="s", utc=True), name="timestamp")
    return out[BAR_COLS]


def _empty_bars() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=float) for c in BAR_COLS},
                        index=pd.DatetimeIndex([], tz="UTC", name="timestamp"))


class BarBuilder:
    """Builds bars from a :class:`TickStore`, caching closed days' 1m bars."""

    def __init__(self, store: TickStore) -> None:
        self.store = store

    def _cache_dir(self, symbol: str) -> Path:
        return self.store.root / "bars1m" / fs_key(symbol)

    @staticmethod
    def _signature(files: list[Path]) -> list:
        sig = []
        for p in files:
            try:
                st = p.stat()
                sig.append([p.name, st.st_size, int(st.st_mtime)])
            except OSError:
                pass
        return sig

    def day(self, symbol: str, day: str, *, now: float | None = None,
            write_cache: bool = True) -> tuple[pd.DataFrame, DaySummary]:
        now = time.time() if now is None else now
        tick_files = [p for p in self.store.day_files(symbol) if p.name.startswith(day)]
        quote_files = [p for p in self.store.day_files(symbol, "quotes") if p.name.startswith(day)]
        xholes = self.store.verified_holes(symbol)
        sig = {"v": CACHE_VERSION, "ticks": self._signature(tick_files),
               "quotes": self._signature(quote_files), "xholes": sorted(list(h) for h in xholes)}
        closed = day < utc_day(now)
        cdir = self._cache_dir(symbol)
        meta_p, bars_p = cdir / f"{day}.json", cdir / f"{day}.csv.gz"
        if closed and meta_p.exists() and bars_p.exists():
            try:
                meta = json.loads(meta_p.read_text(encoding="utf-8"))
                if meta.get("sig") == sig:
                    m1 = pd.read_csv(bars_p, index_col="timestamp", parse_dates=["timestamp"])
                    if m1.index.tz is None:
                        m1.index = m1.index.tz_localize("UTC")
                    s = meta["summary"]
                    return m1[BAR_COLS], DaySummary(s["day"], s["n"], s["first_id"], s["first_ts"],
                                                    s["last_id"], s["last_ts"],
                                                    [tuple(h) for h in s["holes"]])
            except (OSError, ValueError, KeyError):
                pass
        lo, hi = _day_bounds(day)
        ticks = self.store.read(symbol, lo, hi)
        quotes = self.store.read_quotes(symbol, lo, hi)
        m1 = ticks_to_1m(ticks, quotes)
        summ = summarize(ticks, day, xholes)
        if write_cache and closed and summ.n:
            cdir.mkdir(parents=True, exist_ok=True)
            tmp = bars_p.with_name(bars_p.name + ".tmp")
            m1.to_csv(tmp, compression="gzip")
            tmp.replace(bars_p)
            meta_p.write_text(json.dumps({"sig": sig, "summary": summ.to_dict()}), encoding="utf-8")
        return m1, summ

    def build(self, symbol: str, tf_minutes: int, start_ts: float, end_ts: float | None = None,
              *, now: float | None = None, status: dict | None = None,
              write_cache: bool = True) -> pd.DataFrame:
        """Fully-covered closed ``tf_minutes`` bars whose open is in [start_ts, end_ts)."""
        now = time.time() if now is None else now
        end_ts = now if end_ts is None else min(end_ts, now)
        days_avail = sorted({p.name[:10] for p in self.store.day_files(symbol)})
        lo_day, hi_day = utc_day(start_ts - tf_minutes * 60), utc_day(end_ts)
        days = [d for d in days_avail if lo_day <= d <= hi_day]
        if not days:
            return _empty_bars()
        m1s, sums = [], []
        for d in days:
            m1, s = self.day(symbol, d, now=now, write_cache=write_cache)
            m1s.append(m1)
            sums.append(s)
        m1 = pd.concat([x for x in m1s if len(x)]) if any(len(x) for x in m1s) else _empty_bars()
        m1 = m1[~m1.index.duplicated(keep="last")].sort_index()
        status = status if status is not None else self.store.read_status(symbol)
        live_through = status.get("live_through") if status.get("in_sync", True) else None
        runs = coverage_runs(sums, live_through=live_through, live_last_id=status.get("last_trade_id"),
                             exchange_holes=self.store.verified_holes(symbol))
        bars = resample(m1, tf_minutes, runs, now=end_ts)
        if len(bars):
            sec = bars.index.asi8 // 10**9
            bars = bars[(sec >= start_ts - 1e-9) & (sec + tf_minutes * 60 <= end_ts + 1e-9)]
        return bars

    def coverage(self, symbol: str, *, now: float | None = None) -> list[tuple[float, float]]:
        days = sorted({p.name[:10] for p in self.store.day_files(symbol)})
        sums = [self.day(symbol, d, now=now)[1] for d in days]
        st = self.store.read_status(symbol)
        return coverage_runs(sums, live_through=st.get("live_through") if st.get("in_sync", True) else None,
                             live_last_id=st.get("last_trade_id"),
                             exchange_holes=self.store.verified_holes(symbol))

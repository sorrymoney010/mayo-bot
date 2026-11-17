"""Bar source used by the live strategies: pipeline first, REST OHLC fallback.

``PipelineBarSource.get_bars(symbol, tf, lookback, rest_fetch)``:

1. Build fully-covered closed bars from the local tick store.
2. If the collector is live (heartbeat newer than ``stale_seconds``) and the
   local bars alone give ``lookback`` contiguous bars ending at the latest
   closed bar, return them — no REST call.
3. Otherwise call ``rest_fetch()`` (Kraken public OHLC, closed bars only) and
   overlay every fully-covered local bar on it (local wins: it is built from
   the same trades and carries the order-flow columns); REST supplies the
   history/holes. If REST fails, return whatever local bars exist; if there
   are none, re-raise — exactly the old behaviour, so the bot never stalls on
   the pipeline.

The frame always has the gateway's columns (open high low close vwap volume
trades) plus the microstructure columns (NaN on REST-only bars) and ``src``
(``ticks`` | ``ohlc``). ``frame.attrs["pipeline"]`` describes what happened.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from . import SYMBOLS, TIMEFRAMES, canonical
from .bars import BAR_COLS, BarBuilder
from .tickstore import TickStore

_SOURCES: dict[tuple[str, float, tuple[str, ...]], "PipelineBarSource"] = {}
_LOCK = threading.Lock()


def get_source(data_dir: Path | str, *, stale_seconds: float = 600.0,
               symbols: tuple[str, ...] | list[str] = SYMBOLS) -> "PipelineBarSource":
    """Process-wide source per data dir (keeps the per-cycle memo warm)."""
    syms = tuple(s for s in (canonical(x) for x in symbols) if s)
    key = (str(Path(data_dir).resolve()), float(stale_seconds), syms)
    with _LOCK:
        if key not in _SOURCES:
            _SOURCES[key] = PipelineBarSource(data_dir, stale_seconds=stale_seconds, symbols=syms)
        return _SOURCES[key]


class PipelineBarSource:
    def __init__(self, data_dir: Path | str, *, stale_seconds: float = 600.0,
                 symbols: tuple[str, ...] | list[str] = SYMBOLS,
                 now_fn: Callable[[], float] = time.time, memo_seconds: float = 20.0) -> None:
        self.store = TickStore(data_dir)
        self.builder = BarBuilder(self.store)
        self.stale_seconds = stale_seconds
        self.symbols = tuple(s for s in (canonical(x) for x in symbols) if s)
        self.now = now_fn
        self.memo_seconds = memo_seconds
        self._memo: dict[tuple, tuple[float, pd.DataFrame]] = {}
        self.last: dict[str, dict] = {}

    def handles(self, symbol: str | None, tf_minutes: int) -> bool:
        return canonical(symbol) in self.symbols and int(tf_minutes) in TIMEFRAMES

    def feed_status(self, symbol: str) -> dict:
        st = self.store.read_status(symbol)
        lt = st.get("live_through")
        age = self.now() - float(lt) if lt else None
        st["age_s"] = None if age is None else round(age, 1)
        st["fresh"] = bool(age is not None and age <= self.stale_seconds and st.get("in_sync"))
        return st

    def local_bars(self, symbol: str, tf_minutes: int, lookback: int) -> pd.DataFrame:
        now = self.now()
        tfs = tf_minutes * 60
        start = (now // tfs) * tfs - (lookback + 1) * tfs
        return self.builder.build(canonical(symbol), tf_minutes, start, now=now)

    def get_bars(self, symbol: str, tf_minutes: int, lookback: int,
                 rest_fetch: Callable[[], pd.DataFrame]) -> pd.DataFrame:
        sym = canonical(symbol)
        tf = int(tf_minutes)
        now = self.now()
        tfs = tf * 60
        last_closed = int(now // tfs) * tfs - tfs
        mkey = (sym, tf, int(lookback), last_closed)
        hit = self._memo.get(mkey)
        if hit and now - hit[0] < self.memo_seconds:
            return hit[1].copy()

        info: dict = {"symbol": sym, "tf": tf, "at": now}
        try:
            local = self.local_bars(sym, tf, lookback)
        except Exception as exc:  # noqa: BLE001 — a broken store must never block trading
            info["local_error"] = f"{type(exc).__name__}: {exc}"
            local = pd.DataFrame(columns=BAR_COLS)
        feed = self.feed_status(sym)
        info["feed_fresh"] = feed["fresh"]
        info["feed_age_s"] = feed["age_s"]
        info["local_bars"] = int(len(local))
        tail = _contiguous_tail(local, tfs)
        local_ok = (feed["fresh"] and len(tail) >= lookback
                    and int(tail.index[-1].timestamp()) == last_closed)
        if local_ok:
            out = tail.tail(lookback).copy()
            out["src"] = "ticks"
            info.update(source="ticks", rest_bars=0)
        else:
            try:
                rest = rest_fetch()
            except Exception as exc:
                if len(local):
                    out = local.copy()
                    out["src"] = "ticks"
                    info.update(source="ticks_only(rest_failed)", rest_error=f"{type(exc).__name__}")
                    return self._finish(out, info, mkey, now)
                raise
            out = merge_local_over_rest(rest, local)
            n_local = int((out["src"] == "ticks").sum())
            info.update(source="ticks+ohlc" if n_local else "ohlc", rest_bars=int(len(rest)),
                        local_used=n_local,
                        reason=("feed stale" if not feed["fresh"] else
                                f"local history {len(tail)}<{lookback} bars"))
        return self._finish(out.tail(lookback), info, mkey, now)

    def _finish(self, out: pd.DataFrame, info: dict, mkey: tuple, now: float) -> pd.DataFrame:
        info["bars"] = int(len(out))
        info["last_bar"] = out.index[-1].isoformat() if len(out) else None
        out.attrs["pipeline"] = info
        self.last[f"{info['symbol']}@{info['tf']}m"] = info
        self._memo[mkey] = (now, out)
        if len(self._memo) > 64:
            self._memo.pop(next(iter(self._memo)))
        return out.copy()


def _contiguous_tail(bars: pd.DataFrame, tfs: int) -> pd.DataFrame:
    if not len(bars):
        return bars
    sec = bars.index.as_unit("ns").asi8 // 10**9
    breaks = np.nonzero(np.diff(sec) != tfs)[0]
    return bars.iloc[breaks[-1] + 1:] if len(breaks) else bars


def merge_local_over_rest(rest: pd.DataFrame, local: pd.DataFrame) -> pd.DataFrame:
    """REST OHLC frame with every local (tick-built) bar overlaid / appended."""
    r = rest.copy()
    for c in BAR_COLS:
        if c not in r:
            r[c] = np.nan
    r["src"] = "ohlc"
    if local is None or not len(local):
        return r
    loc = local.copy()
    loc["src"] = "ticks"
    if len(r):
        # Only append local bars beyond REST if they continue its grid.
        loc = loc[loc.index >= r.index[0]]
    merged = pd.concat([r[~r.index.isin(loc.index)], loc]).sort_index()
    extra = [c for c in r.columns if c not in merged.columns]
    for c in extra:
        merged[c] = np.nan
    return merged[list(r.columns)]


def pipeline_summary(source: PipelineBarSource) -> str:
    """One log line: tick feed freshness per symbol + last bar source per tf."""
    parts = []
    for sym in source.symbols:
        st = source.feed_status(sym)
        parts.append(f"{sym.split('/')[0]}:feed={'live' if st['fresh'] else 'STALE'}"
                     f"(age={st['age_s']}s id={st.get('last_trade_id')})")
    for key, info in sorted(source.last.items()):
        parts.append(f"{key}={info.get('source')}[{info.get('local_used', info.get('bars'))}/"
                     f"{info.get('bars')} local]")
    return " ".join(parts)

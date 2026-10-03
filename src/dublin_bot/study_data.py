"""Read-only bar loader for the paper research scripts.

Intraday bars prefer ``data/ticks/`` when those files exist. The loader
never writes a ``bars1m`` cache into that directory. Public OHLC caches
(``data/kraken_*_<tf>m.csv``, about 720 bars) are the fallback.

The daily risk-on filter uses the longest daily history available. The
public daily file is the base; tick-built daily bars only extend it or
fill days it does not have. A 120-day tick run must not replace a longer
public daily cache.

Report files are timestamped so a second run on the same day does not
overwrite the first.
"""
from __future__ import annotations

import time
from pathlib import Path

import pandas as pd


def unique_report(directory: Path | str, stem: str, *suffixes: str) -> dict[str, Path]:
    """``stem_YYYY-mm-ddTHHMMSSZ.suffix``, with a numeric tail if that name exists."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if not suffixes:
        suffixes = (".json",)
    stamp = time.strftime("%Y-%m-%dT%H%M%SZ", time.gmtime())
    n = 0
    while True:
        extra = "" if n == 0 else f"_{n}"
        paths = {suf: directory / f"{stem}_{stamp}{extra}{suf}" for suf in suffixes}
        if not any(p.exists() for p in paths.values()):
            return paths
        n += 1


def _ohlc(symbol: str, tf: int, data: Path) -> pd.DataFrame | None:
    base = symbol.replace("/", "")
    parts = []
    for suffix in ("_trades", ""):
        path = data / f"kraken_{base}_{tf}m{suffix}.csv"
        if path.exists():
            df = pd.read_csv(path)
            if len(df):
                parts.append(df)
    if not parts:
        return None
    return (pd.concat(parts).drop_duplicates("time", keep="last")
            .sort_values("time").reset_index(drop=True))


def _ticks(symbol: str, tf: int, data: Path) -> pd.DataFrame | None:
    """Tick-built frame, or None. Does not create ``data/bars1m``."""
    try:
        from .pipeline.history import load_pipeline_bars
        df, _info = load_pipeline_bars(data, symbol, tf, write_cache=False)
    except Exception:
        return None
    if df is None or not len(df):
        return None
    return df


def _time_key(df: pd.DataFrame) -> pd.Series:
    num = pd.to_numeric(df["time"], errors="coerce")
    return num.round().astype("Int64")


def merge_daily(public: pd.DataFrame | None, ticks: pd.DataFrame | None) -> pd.DataFrame | None:
    """Longest daily history. Public bars win on overlap; ticks extend or fill."""
    if public is None or not len(public):
        return None if ticks is None or not len(ticks) else ticks
    if ticks is None or not len(ticks):
        return public
    have = set(int(t) for t in _time_key(public).dropna().tolist())
    tick_time = _time_key(ticks)
    extra = ticks.loc[~tick_time.isin(have)].copy()
    merged = pd.concat([public, extra], ignore_index=True)
    return (merged.drop_duplicates("time", keep="first")
            .sort_values("time").reset_index(drop=True))


def load_daily_history(symbol: str, data: Path | str) -> pd.DataFrame | None:
    """Daily bars for the D1 filter. Public history first, ticks only to extend."""
    data = Path(data)
    return merge_daily(_ohlc(symbol, 1440, data), _ticks(symbol, 1440, data))


def load_study_bars(symbol: str, tf: int, data: Path | str, *,
                    with_daily: bool = True) -> tuple[pd.DataFrame | None, str]:
    """``(frame, source)``. ``source`` is ``ticks``, ``ohlc``, or ``missing``.

    The frame is indicator-enriched and, when a daily series exists, carries
    the daily risk-on column. Callers that need the raw OHLC can ignore it.
    """
    from .backtest_core import add_indicators
    from .daily_filter import attach_d1, riskon_table

    data = Path(data)
    df = _ticks(symbol, tf, data)
    source = "ticks"
    if df is None:
        df = _ohlc(symbol, tf, data)
        source = "ohlc" if df is not None else "missing"
    if df is None:
        return None, "missing"
    df = add_indicators(df)
    if with_daily:
        daily = load_daily_history(symbol, data)
        if daily is not None and len(daily):
            df = attach_d1(df, riskon_table(daily), tf)
    return df, source

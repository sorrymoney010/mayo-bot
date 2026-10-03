"""Pipeline bars in the backtester's frame format (``time`` + OHLCV + flow).

Used by ``scripts/backtest_walkforward.py --source pipeline`` and
``scripts/study_orderflow.py`` so the study replays exactly the bars (and the
order-flow columns) the live strategies receive from ``pipeline.source``.
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import canonical
from .bars import BarBuilder
from .tickstore import TickStore


def load_pipeline_bars(data_dir: Path | str, symbol: str, tf_minutes: int, *,
                       now: float | None = None, write_cache: bool = True) -> tuple[pd.DataFrame, dict]:
    """Latest contiguous run of fully-covered tick-built bars.

    Returns (frame, info). ``frame`` has a ``time`` column (bar open, epoch s)
    plus the bar columns, RangeIndex — what ``backtest_core`` expects.
    """
    sym = canonical(symbol)
    now = time.time() if now is None else now
    bars = BarBuilder(TickStore(data_dir)).build(
        sym, tf_minutes, 0, now=now, write_cache=write_cache)
    info = {"symbol": sym, "tf": tf_minutes, "bars_total": int(len(bars))}
    if not len(bars):
        return pd.DataFrame(), info | {"bars": 0}
    sec = bars.index.asi8 // 10**9
    breaks = np.nonzero(np.diff(sec) != tf_minutes * 60)[0]
    info["holes"] = int(len(breaks))
    if len(breaks):
        bars = bars.iloc[breaks[-1] + 1:]
        sec = sec[breaks[-1] + 1:]
    df = bars.reset_index(drop=True)
    df.insert(0, "time", sec.astype(np.int64))
    info.update(bars=int(len(df)), start=int(sec[0]), end=int(sec[-1]),
                days=round((sec[-1] - sec[0]) / 86400, 1))
    return df, info

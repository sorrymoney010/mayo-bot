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
                       now: float | None = None, write_cache: bool = True,
                       run: str = "latest", min_auto_days: float = 60.0) -> tuple[pd.DataFrame, dict]:
    """Latest contiguous run of fully-covered tick-built bars.

    ``run="longest"`` returns the longest contiguous run instead; ``run="auto"`` keeps the
    latest run when it spans at least ``min_auto_days`` and otherwise falls back to the
    longest (a recent outage hole must not shrink research to a couple of days).

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
    cuts = np.r_[0, breaks + 1, len(sec)]
    pick = len(cuts) - 2                      # latest run
    if run in ("longest", "auto") and len(cuts) > 2:
        longest = int(np.argmax(np.diff(cuts)))
        latest_days = (sec[-1] - sec[cuts[pick]]) / 86400
        if run == "longest" or latest_days < min_auto_days:
            pick = longest
    info["run"] = "latest" if pick == len(cuts) - 2 else "longest"
    bars = bars.iloc[cuts[pick]:cuts[pick + 1]]
    sec = sec[cuts[pick]:cuts[pick + 1]]
    df = bars.reset_index(drop=True)
    df.insert(0, "time", sec.astype(np.int64))
    info.update(bars=int(len(df)), start=int(sec[0]), end=int(sec[-1]),
                days=round((sec[-1] - sec[0]) / 86400, 1))
    return df, info

"""Unit-safe epoch conversion.

pandas 2 datetime indexes are nanosecond-resolution but pandas 3 (and some pandas 2
constructors) use microseconds or seconds. ``asi8 // 10**9`` silently produces wrong
epochs for those, so always go through these helpers.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def epoch_seconds(idx: pd.DatetimeIndex) -> np.ndarray:
    """Whole epoch seconds (int64) for a DatetimeIndex of any resolution."""
    return np.asarray(idx.as_unit("s").asi8, dtype=np.int64)


def series_epoch_seconds(s: pd.Series) -> np.ndarray:
    """Whole epoch seconds (int64) for a datetime-like Series of any resolution."""
    return epoch_seconds(pd.DatetimeIndex(pd.to_datetime(s, utc=True)))

"""Bar loading and a synthetic generator (synthetic data tests plumbing only, never edge)."""
from __future__ import annotations

import numpy as np
import pandas as pd

TZ = "America/New_York"


def load_csv(path: str) -> pd.DataFrame:
    """CSV with columns timestamp,open,high,low,close[,volume]; 1-minute bars. Naive = UTC."""
    df = pd.read_csv(path)
    ts = pd.to_datetime(df["timestamp"], utc=True)
    df = df.drop(columns=["timestamp"])
    df.index = ts.dt.tz_convert(TZ)
    df = df.sort_index()
    if "volume" not in df:
        df["volume"] = 1.0
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def rth(df: pd.DataFrame) -> pd.DataFrame:
    return df.between_time("09:30", "15:59")


def synthetic(days: int = 120, seed: int = 7, start_price: float = 5000.0,
              trend_day_prob: float = 0.25) -> pd.DataFrame:
    """Random-walk 1m RTH bars; a fraction of days get an intraday drift (trend days)."""
    rng = np.random.default_rng(seed)
    idx_days = pd.bdate_range("2024-01-02", periods=days, tz=TZ)
    frames, px = [], start_price
    for d in idx_days:
        mins = pd.date_range(d + pd.Timedelta(hours=9, minutes=30), periods=390, freq="1min")
        t = np.arange(390)
        vol = 0.35 * (1 + 1.6 * np.exp(-t / 40) + 0.6 * np.exp(-(389 - t) / 40))  # U-shape, pts/min
        drift = 0.0
        if rng.random() < trend_day_prob:
            drift = rng.choice([-1, 1]) * 0.05
        rets = rng.normal(drift, vol)
        close = px + np.cumsum(rets)
        open_ = np.concatenate([[px], close[:-1]])
        hi = np.maximum(open_, close) + np.abs(rng.normal(0, vol * 0.4))
        lo = np.minimum(open_, close) - np.abs(rng.normal(0, vol * 0.4))
        frames.append(pd.DataFrame({"open": open_, "high": hi, "low": lo, "close": close,
                                    "volume": rng.integers(200, 2000, 390).astype(float)}, index=mins))
        px = close[-1] + rng.normal(0, 3)
    return pd.concat(frames)

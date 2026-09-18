from __future__ import annotations

import numpy as np
import pandas as pd


def enrich(
    bars: pd.DataFrame,
    *,
    fast_ema: int,
    slow_ema: int,
    regime_ema: int,
    rsi_period: int,
    atr_period: int,
    breakout_lookback: int,
    volume_lookback: int,
) -> pd.DataFrame:
    required = {"open", "high", "low", "close", "volume"}
    missing = required.difference(bars.columns)
    if missing:
        raise ValueError(f"Missing columns: {sorted(missing)}")

    frame = bars.copy().sort_index()
    close = frame["close"].astype(float)
    frame["ema_fast"] = close.ewm(span=fast_ema, adjust=False).mean()
    frame["ema_slow"] = close.ewm(span=slow_ema, adjust=False).mean()
    frame["ema_regime"] = close.ewm(span=regime_ema, adjust=False).mean()

    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / rsi_period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / rsi_period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    frame["rsi"] = (100 - (100 / (1 + rs))).fillna(50)

    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    frame["atr"] = true_range.ewm(alpha=1 / atr_period, adjust=False).mean()
    frame["prior_resistance"] = frame["high"].shift(1).rolling(breakout_lookback).max()
    frame["average_volume"] = frame["volume"].shift(1).rolling(volume_lookback).mean()
    frame["volume_ratio"] = frame["volume"] / frame["average_volume"].replace(0, np.nan)
    return frame


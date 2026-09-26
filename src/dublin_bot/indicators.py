from __future__ import annotations

import math
from typing import Iterable

import numpy as np
import pandas as pd


def _wilder_smooth(values: pd.Series, period: int) -> pd.Series:
    """Wilder / RMA smoothing: first value = SMA, then prev - prev/n + x."""
    arr = values.astype(float).to_numpy(copy=True)
    out = np.full(len(arr), np.nan, dtype=float)
    if period < 1 or len(arr) < period:
        return pd.Series(out, index=values.index)
    # Seed with simple average of the first `period` finite-friendly window.
    seed = np.nanmean(arr[:period])
    if not np.isfinite(seed):
        return pd.Series(out, index=values.index)
    out[period - 1] = seed
    for i in range(period, len(arr)):
        prev = out[i - 1]
        x = arr[i]
        if not np.isfinite(x):
            out[i] = prev
            continue
        # Wilder RMA: (prev*(n-1) + x) / n  ==  prev - prev/n + x/n
        out[i] = prev - (prev / period) + (x / period)
    return pd.Series(out, index=values.index)


def compute_adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Average Directional Index (Wilder) on OHLC series.

    Returns a Series aligned to the input index. Early bars are NaN until the
    DX smooth has enough history (~ 2*period).
    """
    high = high.astype(float)
    low = low.astype(float)
    close = close.astype(float)
    prev_close = close.shift(1)
    prev_high = high.shift(1)
    prev_low = low.shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    up_move = high - prev_high
    down_move = prev_low - low
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    plus_dm = pd.Series(plus_dm, index=high.index, dtype=float)
    minus_dm = pd.Series(minus_dm, index=high.index, dtype=float)

    atr = _wilder_smooth(tr, period)
    smooth_plus = _wilder_smooth(plus_dm, period)
    smooth_minus = _wilder_smooth(minus_dm, period)

    plus_di = 100.0 * (smooth_plus / atr.replace(0, np.nan))
    minus_di = 100.0 * (smooth_minus / atr.replace(0, np.nan))
    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = (100.0 * (plus_di - minus_di).abs() / di_sum).fillna(0.0)
    adx = _wilder_smooth(dx, period)
    return adx


def adx_trend_allowed(
    adx_values: Iterable[float],
    *,
    enter_above: float = 25.0,
    exit_below: float = 20.0,
    initial: bool = False,
) -> bool:
    """Hysteresis sit-out for momentum entries.

    - ADX > ``enter_above`` → allow new BUYs (trend)
    - ADX < ``exit_below`` → deny new BUYs (chop / sit out)
    - between the bands → keep the prior allow/deny state

    Walking the full series makes the decision restart-safe (no in-memory
    latch required across paper-loop engine recreations).
    """
    if enter_above <= exit_below:
        raise ValueError("enter_above must be greater than exit_below")
    allow = bool(initial)
    for raw in adx_values:
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(v):
            continue
        if v > enter_above:
            allow = True
        elif v < exit_below:
            allow = False
        # else: keep prior
    return allow


def fee_edge_ok(
    take_profit_pct: float,
    min_edge_bps: float,
) -> tuple[bool, str]:
    """Require TP / expected move to clear a fee-aware minimum edge.

    Kraken retail Tier 1 ~40 bps maker / 80 bps taker → round-trip taker ~160 bps.
    Gate entries so TP distance is at least ``min_edge_bps`` (default ~100–150)
    before fees; otherwise block with a clear reason.
    """
    min_edge = float(min_edge_bps) / 10_000.0
    tp = float(take_profit_pct)
    if not math.isfinite(tp) or tp <= 0:
        return False, (
            f"Fee gate: invalid TP {tp!r}; need >= {min_edge * 100:.2f}% "
            f"({min_edge_bps:.0f} bps) before fees"
        )
    if tp + 1e-15 < min_edge:
        return False, (
            f"Fee gate: TP {tp * 100:.2f}% < min edge {min_edge * 100:.2f}% "
            f"({min_edge_bps:.0f} bps before fees)"
        )
    return True, (
        f"Fee edge ok: TP {tp * 100:.2f}% >= min edge {min_edge * 100:.2f}%"
    )


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
    adx_period: int = 14,
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

    # Wilder ADX — used as a HARD momentum sit-out in chop (see strategy/engine).
    period = max(int(adx_period), 2)
    frame["adx"] = compute_adx(frame["high"], frame["low"], close, period=period)
    return frame

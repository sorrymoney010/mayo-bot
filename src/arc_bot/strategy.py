"""Opening-range breakout (ORB) with VWAP-side and range-quality filters.

Hypothesis (to be validated on REAL data, not assumed): on days where the first 15 minutes
set a range of moderate size relative to recent daily range, a close beyond that range in the
direction of VWAP has positive expectancy with a defined stop (range midpoint/opposite side).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ORBParams:
    or_minutes: int = 15
    entry_buffer_pts: float = 0.5        # close must clear range by this much
    stop_mode: str = "mid"               # "mid" = range midpoint, "opp" = opposite side
    target_r: float = 2.0
    max_stop_pts: float = 12.0           # skip if stop wider than this
    min_range_frac: float = 0.25         # OR size / avg daily range lower bound
    max_range_frac: float = 0.60         # upper bound (avoid exhausted/news opens)
    entry_cutoff: str = "14:30"
    flatten_at: str = "15:55"
    max_trades_per_day: int = 1
    be_after_r: float = 1.0              # move stop to breakeven after +1R (0 = off)
    risk_per_trade: float = 120.0        # $ at risk per trade (< 15% of $800 drawdown)
    use_vwap_filter: bool = True


def vwap(df):
    tp = (df["high"] + df["low"] + df["close"]) / 3
    v = df["volume"].clip(lower=1)
    return (tp * v).cumsum() / v.cumsum()

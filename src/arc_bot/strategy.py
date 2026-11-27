"""Pluggable intraday strategies. Each exposes: start_day / signal / levels + sizing params.

ORB  : opening-range breakout (momentum).
MeanRev: fade stretches away from VWAP (z-score), target = VWAP, with a trend-day filter.
Hypotheses to be validated on REAL data, not assumed.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def vwap(df):
    tp = (df["high"] + df["low"] + df["close"]) / 3
    v = df["volume"].clip(lower=1)
    return (tp * v).cumsum() / v.cumsum()


@dataclass(frozen=True)
class ORBParams:
    or_minutes: int = 15
    entry_buffer_pts: float = 0.5
    stop_mode: str = "mid"               # "mid" = range midpoint, "opp" = opposite side
    target_r: float = 2.0
    max_stop_pts: float = 12.0
    min_range_frac: float = 0.25
    max_range_frac: float = 0.60
    entry_cutoff: str = "14:30"
    flatten_at: str = "15:55"
    max_trades_per_day: int = 1
    be_after_r: float = 1.0
    risk_per_trade: float = 120.0
    use_vwap_filter: bool = True
    name: str = "orb"

    def start_day(self, g, avg_rng):
        orb = g.iloc[: self.or_minutes]
        if len(orb) < self.or_minutes or avg_rng is None:
            return None
        hi, lo = float(orb["high"].max()), float(orb["low"].min())
        if not (self.min_range_frac <= (hi - lo) / avg_rng <= self.max_range_frac):
            return None
        return dict(hi=hi, lo=lo, mid=(hi + lo) / 2, vw=vwap(g), start=self.or_minutes, avg_rng=avg_rng)

    def signal(self, i, ts, b, ctx, g):
        c, v = b["close"], ctx["vw"].loc[ts]
        if c > ctx["hi"] + self.entry_buffer_pts and (not self.use_vwap_filter or c > v):
            return 1
        if c < ctx["lo"] - self.entry_buffer_pts and (not self.use_vwap_filter or c < v):
            return -1
        return 0

    def levels(self, side, entry, ctx):
        stop = ctx["mid"] if self.stop_mode == "mid" else (ctx["lo"] if side > 0 else ctx["hi"])
        risk = abs(entry - stop)
        return stop, entry + side * self.target_r * risk


@dataclass(frozen=True)
class MRParams:
    z_entry: float = 2.2          # |close - VWAP| / rolling std of that deviation
    z_window: int = 60
    warmup_minutes: int = 45      # let VWAP/vol settle after the open
    atr_window: int = 20
    stop_atr_mult: float = 3.0    # stop = entry -/+ mult * ATR(1m); also bounded by max_stop_pts
    max_stop_pts: float = 12.0
    min_target_pts: float = 1.5   # skip if VWAP is too close to pay costs
    trend_day_frac: float = 0.55  # skip once day range-from-open exceeds this x avg daily range
    entry_cutoff: str = "14:30"
    flatten_at: str = "15:55"
    max_trades_per_day: int = 2
    be_after_r: float = 0.0
    risk_per_trade: float = 120.0
    require_reversal_bar: bool = True
    name: str = "meanrev"

    def start_day(self, g, avg_rng):
        if avg_rng is None or len(g) < self.warmup_minutes + 5:
            return None
        vw = vwap(g)
        dev = g["close"] - vw
        sd = dev.rolling(self.z_window, min_periods=20).std()
        z = dev / sd
        tr = (g["high"] - g["low"]).rolling(self.atr_window, min_periods=5).mean()
        return dict(vw=vw, z=z, atr=tr, start=self.warmup_minutes, avg_rng=avg_rng,
                    open=float(g["open"].iloc[0]))

    def signal(self, i, ts, b, ctx, g):
        z, atr = ctx["z"].loc[ts], ctx["atr"].loc[ts]
        if not np.isfinite(z) or not np.isfinite(atr):
            return 0
        sofar = g.loc[:ts]
        if (sofar["high"].max() - sofar["low"].min()) > self.trend_day_frac * ctx["avg_rng"]:
            return 0  # trend day: do not fade
        side = 0
        if z <= -self.z_entry and (not self.require_reversal_bar or b["close"] > b["open"]):
            side = 1
        elif z >= self.z_entry and (not self.require_reversal_bar or b["close"] < b["open"]):
            side = -1
        if side:
            ctx["sig_vwap"], ctx["sig_atr"] = float(ctx["vw"].loc[ts]), float(atr)
        return side

    def levels(self, side, entry, ctx):
        stop_pts = min(self.stop_atr_mult * ctx["sig_atr"], self.max_stop_pts)
        target = ctx["sig_vwap"]
        if (target - entry) * side < self.min_target_pts:
            return entry - side * 1e-9, entry  # degenerate -> rejected (risk 0)
        return entry - side * stop_pts, target

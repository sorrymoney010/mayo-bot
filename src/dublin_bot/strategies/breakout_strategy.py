"""Defined-risk momentum breakout sleeve (ported from dual-venue-momentum-bot).

Signal (15m typical):
  close > N-bar prior high AND volume > N-bar average volume.
  Lookback excludes the current (breakout) bar — same as the TS reference.

Risk (sleeve defaults; configured via Settings):
  ~1% equity per trade, stop ~2% below entry, take-profit ~4% (2R).
  Paper exits use Settings.stop_loss_pct / take_profit_pct (engine protective).

ADX: intentionally NOT applied inside this strategy. Breakouts key off
volume + high break; the engine ADX sit-out only runs for strategy=momentum.
Fee min-edge still applies in the engine — 4% TP clears MIN_EDGE_BPS=100.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from dublin_bot.config import Settings
from dublin_bot.models import Action, Signal


@dataclass(frozen=True)
class BreakoutParams:
    lookback_bars: int = 20
    volume_avg_bars: int = 20
    stop_pct: float = 0.02  # fraction of price


def detect_breakout(
    bars: pd.DataFrame,
    params: BreakoutParams,
) -> tuple[bool, str, float, float, float]:
    """Return (fired, reason, price, lookback_high, volume_ratio).

    Matches dual-venue ``detectBreakout``: prior window is bars[-(N+1):-1].
    """
    lookback = int(params.lookback_bars)
    vol_avg = int(params.volume_avg_bars)
    need = max(lookback, vol_avg) + 1
    if bars is None or len(bars) < need:
        return False, f"Not enough bars ({0 if bars is None else len(bars)} < {need})", 0.0, 0.0, 0.0

    frame = bars.sort_index()
    current = frame.iloc[-1]
    price = float(current["close"])
    volume = float(current["volume"])

    prior = frame.iloc[-(lookback + 1) : -1]
    if len(prior) < lookback:
        return False, "Incomplete lookback window", price, 0.0, 0.0

    lookback_high = float(prior["high"].max())
    vol_window = frame.iloc[-(vol_avg + 1) : -1]
    avg_volume = float(vol_window["volume"].mean()) if len(vol_window) else 0.0
    if not math.isfinite(avg_volume) or avg_volume <= 0:
        return False, "Average volume unavailable", price, lookback_high, 0.0

    volume_ratio = volume / avg_volume
    broke_out = price > lookback_high
    volume_ok = volume > avg_volume

    if broke_out and volume_ok:
        reason = (
            f"Breakout: close {price:.6g} > {lookback}-bar high {lookback_high:.6g}; "
            f"vol {volume:.4g} > avg {avg_volume:.4g} (ratio={volume_ratio:.2f})"
        )
        return True, reason, price, lookback_high, volume_ratio

    parts: list[str] = []
    if not broke_out:
        parts.append(f"close {price:.6g} <= {lookback}-bar high {lookback_high:.6g}")
    if not volume_ok:
        parts.append(f"vol {volume:.4g} <= avg {avg_volume:.4g}")
    return False, "No breakout: " + "; ".join(parts), price, lookback_high, volume_ratio


def size_breakout_quantity(
    *,
    equity: float,
    risk_pct: float,
    entry_price: float,
    stop_price: float,
) -> float:
    """Size so stop-distance risk ≈ risk_pct of equity (TS sizePosition).

    quantity = (equity * risk_pct) / |entry - stop|
    Crypto: truncate to 6 decimal places. Returns 0 when unaffordable / invalid.
    """
    if equity <= 0 or risk_pct <= 0 or risk_pct > 1.0:
        return 0.0
    if entry_price <= 0:
        return 0.0
    stop_distance = abs(entry_price - stop_price)
    if stop_distance <= 0:
        return 0.0
    risk_dollars = equity * risk_pct
    quantity = risk_dollars / stop_distance
    return math.floor(quantity * 1e6) / 1e6


def _simple_atr(frame: pd.DataFrame, period: int = 14) -> float:
    if len(frame) < 2:
        return float("nan")
    prev = frame["close"].shift(1)
    tr = pd.concat(
        [
            (frame["high"] - frame["low"]).abs(),
            (frame["high"] - prev).abs(),
            (frame["low"] - prev).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = float(tr.ewm(alpha=1 / max(period, 2), adjust=False).mean().iloc[-1])
    return atr if math.isfinite(atr) and atr > 0 else float("nan")


class BreakoutStrategy:
    """N-bar high + volume breakout with fixed-% stop for risk sizing."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _params(self) -> BreakoutParams:
        s = self.settings
        lookback = int(getattr(s, "breakout_lookback", 20) or 20)
        vol_avg = int(
            getattr(s, "breakout_vol_avg", None)
            or getattr(s, "volume_lookback", 20)
            or 20
        )
        stop_pct = float(getattr(s, "stop_loss_pct", 0.02) or 0.02)
        return BreakoutParams(
            lookback_bars=lookback,
            volume_avg_bars=vol_avg,
            stop_pct=stop_pct,
        )

    def evaluate(self, bars: pd.DataFrame, in_position: bool = False) -> Signal:
        params = self._params()
        atr = _simple_atr(bars, int(getattr(self.settings, "atr_period", 14) or 14))
        fired, reason, price, _high, _vr = detect_breakout(bars, params)

        if in_position:
            # Exits are paper stop/TP (2%/4%) or live brackets — not signal fades.
            hold_price = price if price > 0 else (
                float(bars["close"].iloc[-1]) if bars is not None and len(bars) else 0.0
            )
            return Signal(
                Action.WAIT,
                55,
                "Holding breakout; exits via stop/TP sleeve",
                hold_price,
                atr if math.isfinite(atr) else None,
            )

        if not fired:
            return Signal(
                Action.WAIT,
                10,
                reason,
                price,
                atr if math.isfinite(atr) else None,
            )

        stop_price = max(0.0, price * (1.0 - params.stop_pct))
        if stop_price <= 0 or stop_price >= price:
            return Signal(Action.WAIT, 5, "Invalid stop distance", price, atr)

        return Signal(
            Action.BUY,
            80,
            reason,
            price,
            atr if math.isfinite(atr) else None,
            stop_price,
        )

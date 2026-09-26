"""Rule-based classic pattern / Elliott-lite strategy.

This is intentionally **not** discretionary Elliott Wave counting. It implements
two OHLC geometric patterns with explicit entry and invalidation rules:

1. **Double bottom + neckline break (primary)**
   - Two swing lows within ``pattern_dbl_tol_pct`` of each other.
   - Neckline = highest high between the two bottoms.
   - **BUY** when close breaks above the neckline by ``pattern_min_break_pct``
     (or ATR * ``pattern_min_break_atr`` when that knob is > 0).
   - **Invalidation / stop**: below the lower of the two bottoms (encoded in
     ``stop_price`` and as SELL reason while in position).

2. **Bullish flag breakout (secondary)**
   - Pole: a sharp advance of at least ``pattern_flag_pole_pct`` over a short
     window ending before the consolidation.
   - Flag: a subsequent tight range (high-low of flag / price <=
     ``pattern_flag_max_range_pct``) lasting ``pattern_flag_bars`` bars.
   - **BUY** when close breaks above the flag high.
   - **Invalidation / stop**: below the flag low.

Wire via ``settings.strategy == "pattern"`` or ``"elliott_lite"`` (aliases).
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from dublin_bot.config import Settings
from dublin_bot.models import Action, Signal


def _atr(frame: pd.DataFrame, period: int) -> float:
    prev = frame["close"].shift(1)
    tr = pd.concat(
        [
            (frame["high"] - frame["low"]).abs(),
            (frame["high"] - prev).abs(),
            (frame["low"] - prev).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = float(tr.ewm(alpha=1 / max(period, 1), adjust=False).mean().iloc[-1])
    return atr if math.isfinite(atr) and atr > 0 else float("nan")


def _pivot_lows(low: np.ndarray, strength: int) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    n = len(low)
    strength = max(1, strength)
    for i in range(strength, n - strength):
        window = low[i - strength : i + strength + 1]
        if low[i] <= float(window.min()):
            out.append((i, float(low[i])))
    return out


def _break_above(price: float, level: float, atr: float, min_atr: float, min_pct: float) -> bool:
    distance = price - level
    if distance <= 0:
        return False
    if min_atr <= 0 and min_pct <= 0:
        return distance / max(level, 1e-12) >= 0.001
    ok = True
    if min_atr > 0:
        ok = ok and atr > 0 and distance >= atr * min_atr
    if min_pct > 0:
        ok = ok and distance / max(level, 1e-12) >= min_pct
    return ok


class PatternStrategy:
    """Double-bottom neckline break and/or bullish flag breakout."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._invalidation: Optional[float] = None
        self._active_pattern: Optional[str] = None

    def evaluate(self, bars: pd.DataFrame, in_position: bool = False) -> Signal:
        s = self.settings
        if bars is None or len(bars) < 40:
            return Signal(Action.WAIT, 0, "Not enough bars for pattern strategy", 0.0)
        if not {"open", "high", "low", "close"}.issubset(bars.columns):
            return Signal(Action.WAIT, 0, "OHLC required for pattern strategy", 0.0)

        frame = bars.copy()
        high = frame["high"].astype(float).to_numpy()
        low = frame["low"].astype(float).to_numpy()
        close = frame["close"].astype(float).to_numpy()
        price = float(close[-1])
        atr = _atr(frame, int(s.atr_period))
        if not math.isfinite(atr) or atr <= 0:
            atr = max(price * 0.01, 1e-8)

        if in_position:
            inv = self._invalidation
            if inv is not None and price < inv:
                reason = (
                    f"Pattern invalidation: close below {inv:.6g}"
                    + (f" ({self._active_pattern})" if self._active_pattern else "")
                )
                self._invalidation = None
                self._active_pattern = None
                return Signal(Action.SELL, 90, reason, price, atr)
            # Also exit if price loses the slow structure: close below recent swing low.
            recent_low = float(low[-min(10, len(low)) :].min())
            if price < recent_low * 0.995:
                self._invalidation = None
                self._active_pattern = None
                return Signal(
                    Action.SELL, 80,
                    f"Pattern exit: broke recent swing low {recent_low:.6g}",
                    price, atr,
                )
            return Signal(
                Action.WAIT, 55,
                f"Holding pattern trade; invalidation={inv}",
                price, atr,
            )

        # Flat: look for entries. Prefer double bottom, then flag.
        dbl = self._double_bottom_signal(high, low, close, price, atr, s)
        if dbl is not None:
            return dbl
        flag = self._bullish_flag_signal(high, low, close, price, atr, s)
        if flag is not None:
            return flag
        return Signal(Action.WAIT, 8, "No double-bottom or bullish-flag breakout", price, atr)

    def _double_bottom_signal(
        self,
        high: np.ndarray,
        low: np.ndarray,
        close: np.ndarray,
        price: float,
        atr: float,
        s: Settings,
    ) -> Optional[Signal]:
        strength = max(1, int(getattr(s, "pattern_pivot_strength", 2)))
        pivots = _pivot_lows(low, strength)
        if len(pivots) < 2:
            return None

        tol = float(s.pattern_dbl_tol_pct)
        min_sep = max(3, strength * 2)
        # Search newest pairs first.
        for j in range(len(pivots) - 1, 0, -1):
            i2, low2 = pivots[j]
            for i in range(j - 1, -1, -1):
                i1, low1 = pivots[i]
                if i2 - i1 < min_sep:
                    continue
                mid = (low1 + low2) / 2.0
                if mid <= 0:
                    continue
                if abs(low1 - low2) / mid > tol:
                    continue
                # Neckline: max high strictly between the two bottoms.
                if i2 <= i1 + 1:
                    continue
                neckline = float(high[i1 + 1 : i2].max())
                trough = min(low1, low2)
                if neckline <= trough:
                    continue
                # Break must be fresh-ish: prior close at/below neckline.
                prior = float(close[-2]) if len(close) >= 2 else price
                if not _break_above(
                    price, neckline, atr,
                    float(s.pattern_min_break_atr),
                    float(s.pattern_min_break_pct),
                ):
                    continue
                if prior > neckline:
                    # Already broke earlier; only fire on the break bar.
                    continue
                stop = max(0.0, trough - atr * 0.1)
                self._invalidation = trough
                self._active_pattern = "double_bottom"
                return Signal(
                    Action.BUY,
                    78,
                    (
                        f"Double-bottom neckline break above {neckline:.6g} "
                        f"(bottoms {low1:.6g}/{low2:.6g}; invalidate < {trough:.6g})"
                    ),
                    price,
                    atr,
                    stop,
                )
        return None

    def _bullish_flag_signal(
        self,
        high: np.ndarray,
        low: np.ndarray,
        close: np.ndarray,
        price: float,
        atr: float,
        s: Settings,
    ) -> Optional[Signal]:
        flag_bars = max(3, int(s.pattern_flag_bars))
        pole_look = max(3, int(s.pattern_flag_pole_bars))
        if len(close) < pole_look + flag_bars + 2:
            return None

        flag_slice = slice(-(flag_bars + 1), -1)  # consolidation excluding current bar
        pole_end = len(close) - flag_bars - 1
        pole_start = max(0, pole_end - pole_look)
        if pole_end <= pole_start:
            return None

        pole_move = (float(close[pole_end - 1]) - float(close[pole_start])) / max(
            float(close[pole_start]), 1e-12
        )
        if pole_move < float(s.pattern_flag_pole_pct):
            return None

        flag_high = float(high[flag_slice].max())
        flag_low = float(low[flag_slice].min())
        flag_range = (flag_high - flag_low) / max(price, 1e-12)
        if flag_range > float(s.pattern_flag_max_range_pct):
            return None

        prior = float(close[-2])
        if prior > flag_high:
            return None
        if not _break_above(
            price, flag_high, atr,
            float(s.pattern_min_break_atr),
            float(s.pattern_min_break_pct),
        ):
            return None

        stop = max(0.0, flag_low - atr * 0.1)
        self._invalidation = flag_low
        self._active_pattern = "bullish_flag"
        return Signal(
            Action.BUY,
            72,
            (
                f"Bullish flag breakout above {flag_high:.6g} "
                f"(pole +{pole_move*100:.1f}%; invalidate < {flag_low:.6g})"
            ),
            price,
            atr,
            stop,
        )

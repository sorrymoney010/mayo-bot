"""Support/Resistance flip sleeve.

Rule-based swing-pivot strategy (no vibes):

1. Detect swing pivot highs/lows with ``sr_pivot_strength`` bars on each side.
2. **Bullish entry (resistance → support flip):**
   - A prior pivot high acts as resistance.
   - Price breaks above that level by at least ``sr_min_break_atr`` * ATR
     OR ``sr_min_break_pct`` of price (whichever threshold is configured > 0;
     both must pass when both are > 0).
   - Price then retests the broken level from above (pullback within a small
     tolerance of the flip level) and closes back above it → BUY reclaim.
3. **Exit while in position (support → resistance flip):**
   - A prior pivot low that failed as support (close below by the break
     threshold) → SELL with clear reason.

Invalidation for a fresh BUY is encoded in ``stop_price`` (below the flip
level / last swing low). Lookback window: ``sr_lookback`` bars.
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
    atr = float(tr.ewm(alpha=1 / period, adjust=False).mean().iloc[-1])
    return atr if math.isfinite(atr) and atr > 0 else float("nan")


def _pivot_highs(high: np.ndarray, strength: int) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    n = len(high)
    for i in range(strength, n - strength):
        window = high[i - strength : i + strength + 1]
        if high[i] >= window.max() and np.sum(window == high[i]) == 1:
            out.append((i, float(high[i])))
        elif high[i] >= window.max():
            # Tie: still accept leftmost equal max as a pivot.
            if i == i - strength + int(np.argmax(window)):
                out.append((i, float(high[i])))
    return out


def _pivot_lows(low: np.ndarray, strength: int) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    n = len(low)
    for i in range(strength, n - strength):
        window = low[i - strength : i + strength + 1]
        if low[i] <= window.min() and np.sum(window == low[i]) == 1:
            out.append((i, float(low[i])))
        elif low[i] <= window.min():
            if i == i - strength + int(np.argmin(window)):
                out.append((i, float(low[i])))
    return out


def _break_ok(price: float, level: float, atr: float, min_atr: float, min_pct: float, *, above: bool) -> bool:
    """Return True if price has broken level by configured ATR and/or % thresholds."""
    if above:
        distance = price - level
    else:
        distance = level - price
    if distance <= 0:
        return False
    need_atr = min_atr > 0
    need_pct = min_pct > 0
    if not need_atr and not need_pct:
        # Default: any clear break of at least 0.1% if no knobs set.
        return distance / max(level, 1e-12) >= 0.001
    ok = True
    if need_atr:
        ok = ok and (atr > 0 and distance >= atr * min_atr)
    if need_pct:
        ok = ok and (distance / max(level, 1e-12) >= min_pct)
    return ok


class SRFlipStrategy:
    """Resistance→support flip BUY; support→resistance flip SELL."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def evaluate(self, bars: pd.DataFrame, in_position: bool = False) -> Signal:
        s = self.settings
        if bars is None or len(bars) < max(30, int(s.sr_pivot_strength) * 4 + 5):
            return Signal(Action.WAIT, 0, "Not enough bars for S/R flip", 0.0)

        lookback = min(int(s.sr_lookback), len(bars))
        frame = bars.iloc[-lookback:].copy()
        if not {"open", "high", "low", "close"}.issubset(frame.columns):
            return Signal(Action.WAIT, 0, "OHLC required for S/R flip", 0.0)

        high = frame["high"].astype(float).to_numpy()
        low = frame["low"].astype(float).to_numpy()
        close = frame["close"].astype(float).to_numpy()
        price = float(close[-1])
        atr = _atr(frame, int(s.atr_period))
        if not math.isfinite(atr) or atr <= 0:
            atr = max(price * 0.01, 1e-8)

        strength = max(1, int(s.sr_pivot_strength))
        # Pivots need confirmed right-side bars — last `strength` bars are not pivots yet.
        piv_h = _pivot_highs(high, strength)
        piv_l = _pivot_lows(low, strength)

        if in_position:
            return self._evaluate_exit(price, atr, close, piv_l, s)

        return self._evaluate_entry(price, atr, high, low, close, piv_h, piv_l, s)

    def _evaluate_exit(
        self,
        price: float,
        atr: float,
        close: np.ndarray,
        piv_l: list[tuple[int, float]],
        s: Settings,
    ) -> Signal:
        if not piv_l:
            return Signal(Action.WAIT, 40, "In position; no pivot-low support map yet", price, atr)

        # Most recent confirmed pivot low as support candidate.
        idx, level = piv_l[-1]
        # Only treat as support→resistance if price traded at/above it after the pivot,
        # then broke below.
        after = close[idx + 1 :] if idx + 1 < len(close) else close[-1:]
        held_as_support = bool(len(after) and after.max() >= level)
        if held_as_support and _break_ok(
            price, level, atr, float(s.sr_min_break_atr), float(s.sr_min_break_pct), above=False
        ):
            return Signal(
                Action.SELL,
                85,
                f"S/R flip exit: support→resistance break below {level:.6g}",
                price,
                atr,
            )
        return Signal(Action.WAIT, 55, f"Holding; support {level:.6g} intact", price, atr)

    def _evaluate_entry(
        self,
        price: float,
        atr: float,
        high: np.ndarray,
        low: np.ndarray,
        close: np.ndarray,
        piv_h: list[tuple[int, float]],
        piv_l: list[tuple[int, float]],
        s: Settings,
    ) -> Signal:
        if not piv_h:
            return Signal(Action.WAIT, 5, "No resistance pivots detected", price, atr)

        min_atr = float(s.sr_min_break_atr)
        min_pct = float(s.sr_min_break_pct)
        n = len(close)
        # Prefer the most recent pivot high that was broken then retested.
        for idx, level in reversed(piv_h):
            # Need room after the pivot for break + retest + reclaim.
            if idx >= n - (s.sr_pivot_strength + 3):
                continue
            post = slice(idx + 1, n)
            post_high = high[post]
            post_low = low[post]
            post_close = close[post]
            if len(post_close) < 3:
                continue

            # Find first bar that clearly broke above resistance.
            broke_i: Optional[int] = None
            for j, (ph, pc) in enumerate(zip(post_high, post_close)):
                if _break_ok(float(max(ph, pc)), level, atr, min_atr, min_pct, above=True):
                    broke_i = j
                    break
            if broke_i is None:
                continue

            after_break_low = post_low[broke_i:]
            after_break_close = post_close[broke_i:]
            if len(after_break_close) < 2:
                continue

            # Retest: dipped back near the flip level (within 0.5 ATR or 0.5%) from above.
            tol = max(atr * 0.5, level * 0.005)
            retest_j: Optional[int] = None
            for j in range(1, len(after_break_low)):
                # Must have been above after the break, then tag the level.
                if after_break_close[:j].max() > level and after_break_low[j] <= level + tol:
                    retest_j = j
                    break
            if retest_j is None:
                continue

            # Reclaim: current close back above the flip level after the retest.
            # Prefer reclaim on the latest bar.
            if price > level and after_break_close[retest_j:].max() >= price:
                # Ensure we actually retested (not just stayed above without a tag).
                tagged = after_break_low[retest_j] <= level + tol
                if not tagged:
                    continue
                # Current bar should be the reclaim (close above after dipping).
                recent_low = float(after_break_low[retest_j:].min())
                if recent_low > level + tol:
                    # Never came back — not a flip reclaim, skip.
                    continue
                stop_price = max(0.0, min(level, recent_low) - atr * float(s.atr_stop_multiplier) * 0.25)
                stop_price = max(0.0, level - atr * float(s.atr_stop_multiplier))
                return Signal(
                    Action.BUY,
                    75,
                    f"S/R flip BUY: resistance→support reclaim above {level:.6g}",
                    price,
                    atr,
                    stop_price,
                )

        # Fresh resistance break on the current bar (no retest required):
        # prior close at/below a confirmed pivot high, current close clearly above.
        prior = float(close[-2])
        for idx, level in reversed(piv_h):
            if prior <= level < price and _break_ok(price, level, atr, min_atr, min_pct, above=True):
                stop_price = max(0.0, level - atr * float(s.atr_stop_multiplier))
                return Signal(
                    Action.BUY,
                    65,
                    f"S/R flip BUY: fresh resistance break/reclaim of {level:.6g}",
                    price,
                    atr,
                    stop_price,
                )

        _ = piv_l  # reserved for future confluence filters
        return Signal(Action.WAIT, 10, "No resistance→support flip reclaim", price, atr)

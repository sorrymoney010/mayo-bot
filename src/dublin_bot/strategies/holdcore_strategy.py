"""Hold-core paper signal.

Long while the daily risk-on filter is on. Flat when it is off or unknown.
No EMA cross, no hard stop, no take-profit. Paper only — the factory still
requires the three locks before any order path runs.
"""
from __future__ import annotations

import math

import pandas as pd

from dublin_bot.backtest_core import add_indicators, holdcore_signals
from dublin_bot.config import Settings
from dublin_bot.models import Action, Signal

MIN_BARS = 50


class HoldCoreStrategy:
    name = "hold_core"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.last_frame: pd.DataFrame | None = None
        self.d1_blocked = False

    def params(self) -> dict:
        return {"d1": True, "min_bars": MIN_BARS}

    def evaluate(self, bars: pd.DataFrame | None, in_position: bool = False) -> Signal:
        self.d1_blocked = False
        self.last_frame = None
        n = 0 if bars is None else len(bars)
        if bars is None or n < MIN_BARS:
            return Signal(Action.WAIT, 0, f"Not enough bars for hold-core ({n} < {MIN_BARS})", 0.0)
        d = add_indicators(bars.sort_index())
        from dublin_bot.daily_filter import d1_reason, live_d1

        d = live_d1(self, d, int(getattr(self.settings, "timeframe_minutes", 240) or 240))
        sig = holdcore_signals(d, self.params())
        self.last_frame = d
        i = len(d) - 1
        price = float(d["close"].iloc[i])
        atr = float(d["atr"].iloc[i]) if "atr" in d else float("nan")
        atr = atr if math.isfinite(atr) and atr > 0 else None
        on = bool(sig["d1_ok"][i])
        self.d1_blocked = not on
        why = d1_reason(d, i)
        if in_position and not on:
            return Signal(Action.SELL, 80, f"Hold-core exit: {why}", price, atr)
        if in_position:
            return Signal(Action.WAIT, 50, f"Holding core: {why}", price, atr)
        if on:
            return Signal(Action.BUY, 70, f"Hold-core entry: {why}", price, atr)
        return Signal(Action.WAIT, 10, f"Hold-core flat: {why}", price, atr)

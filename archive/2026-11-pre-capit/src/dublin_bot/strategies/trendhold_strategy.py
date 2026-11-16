"""Trend-hold 4h signal (third PAPER sleeve, see ``trendhold_sleeve``).

Identical rules to the backtest family ``trendhold`` (``backtest_core.trendhold_signals``),
evaluated on the last CLOSED bar:

Entry:  daily risk-on filter (D1) on AND close > EMA100 AND EMA20 > EMA100
        -> the sleeve buys at market on the next cycle (= next bar's open).
Exit:   the first close below EMA100 -> market sell. No hard stop, no TP.
"""
from __future__ import annotations

import math

import pandas as pd

from dublin_bot.backtest_core import add_indicators, trendhold_signals
from dublin_bot.config import Settings
from dublin_bot.models import Action, Signal

MIN_BARS = 120


class TrendHoldStrategy:
    name = "trendhold_4h"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.last_frame: pd.DataFrame | None = None

    def params(self) -> dict:
        s = self.settings
        return {"ema_fast": int(getattr(s, "trendhold_ema_fast", 20)),
                "ema_slow": int(getattr(s, "trendhold_ema_slow", 100)),
                "d1": bool(getattr(s, "trendhold_daily_filter", True))}

    def evaluate(self, bars: pd.DataFrame | None, in_position: bool = False) -> Signal:
        self.d1_blocked = False
        self.last_frame = None
        n = 0 if bars is None else len(bars)
        if bars is None or n < MIN_BARS:
            return Signal(Action.WAIT, 0, f"Not enough bars for trend-hold ({n} < {MIN_BARS})", 0.0)
        d = add_indicators(bars.sort_index())
        p = self.params()
        if p["d1"] and not in_position:
            from dublin_bot.daily_filter import live_d1
            d = live_d1(self, d, int(self.settings.timeframe_minutes))
        else:
            p = {**p, "d1": False}
        sig = trendhold_signals(d, p)
        self.last_frame = d
        i = len(d) - 1
        # the raw rules fired but the daily filter stopped it -> shadow-trade candidate
        self.d1_blocked = bool(p["d1"] and not sig["d1_ok"][i]
                               and trendhold_signals(d, {**p, "d1": False})["entry"][i])
        price = float(d["close"].iloc[i])
        ef, es = float(sig["ema_fast"][i]), float(sig["ema_slow"][i])
        atr = float(d["atr"].iloc[i])
        atr = atr if math.isfinite(atr) and atr > 0 else None
        txt = f"close={price:.6g} ema{p['ema_fast']}={ef:.6g} ema{p['ema_slow']}={es:.6g}"
        if in_position:
            if sig["exit"][i]:
                return Signal(Action.SELL, 80, f"Trend-hold exit: close < ema{p['ema_slow']} ({txt})", price, atr)
            return Signal(Action.WAIT, 50, f"Holding trend: {txt}", price, atr)
        if sig["entry"][i]:
            return Signal(Action.BUY, 75, f"Trend-hold entry: D1 on, close > ema{p['ema_slow']}, "
                          f"ema{p['ema_fast']} > ema{p['ema_slow']} ({txt})", price, atr)
        why = []
        if not price > es:
            why.append(f"close <= ema{p['ema_slow']}")
        if not ef > es:
            why.append(f"ema{p['ema_fast']} <= ema{p['ema_slow']}")
        if p["d1"] and not sig["d1_ok"][i]:
            from dublin_bot.daily_filter import d1_reason
            why.append(d1_reason(d, i))
        return Signal(Action.WAIT, 10, "No trend-hold entry: " + "; ".join(why or ["warming up"]) + f" ({txt})",
                      price, atr)

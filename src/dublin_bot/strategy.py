from __future__ import annotations

import math
import pandas as pd

from .config import Settings
from .indicators import enrich
from .models import Action, Signal


class TrendBreakoutStrategy:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def evaluate(self, bars: pd.DataFrame, in_position: bool = False) -> Signal:
        s = self.settings
        frame = enrich(
            bars,
            fast_ema=s.fast_ema,
            slow_ema=s.slow_ema,
            regime_ema=s.regime_ema,
            rsi_period=s.rsi_period,
            atr_period=s.atr_period,
            breakout_lookback=s.breakout_lookback,
            volume_lookback=s.volume_lookback,
        ).dropna()
        if frame.empty:
            return Signal(Action.WAIT, 0, "Not enough completed bars", 0.0)

        row = frame.iloc[-1]
        price = float(row["close"])
        atr = float(row["atr"])
        if not math.isfinite(atr) or atr <= 0:
            return Signal(Action.WAIT, 0, "ATR unavailable", price)

        if in_position:
            if price < float(row["ema_slow"]):
                return Signal(Action.SELL, 90, "Price closed below slow trend EMA", price, atr)
            return Signal(Action.WAIT, 60, "Position remains above slow trend EMA", price, atr)

        checks = {
            # Momentum is the hard gate: RSI must sit inside the tradable band
            # (not overbought, not washed out). In a range-bound / grinding market
            # this is the actionable mean-reversion trigger.
            "momentum": s.rsi_min <= float(row["rsi"]) <= s.rsi_max,
        }
        # Advisory only — they shape the score and the note, but never block an
        # entry on their own. This is what lets the bot trade in choppy/flat
        # sessions instead of waiting forever for a clean trend or a print at
        # the prior resistance.
        advisory = {
            "regime": price > float(row["ema_regime"]),
            "trend": float(row["ema_fast"]) > float(row["ema_slow"]),
            "breakout": price >= float(row["prior_resistance"]) - atr,
            "volume": float(row["volume_ratio"]) >= s.min_volume_ratio,
        }
        score = (sum(checks.values()) * 20) + (sum(advisory.values()) * 5)
        failed = [name for name, passed in checks.items() if not passed]

        # Fire when momentum passes. Risk limits (size, orders/day, daily-loss,
        # drawdown) are still enforced downstream in risk.py.
        if not failed:
            stop_price = max(0.0, price - atr * s.atr_stop_multiplier)
            note = "Momentum confirmed"
            if advisory["breakout"]:
                note += " + at resistance"
            if not advisory["regime"]:
                note += " (counter-trend)"
            if not advisory["volume"]:
                note += "; low volume (advisory)"
            return Signal(Action.BUY, score, note, price, atr, stop_price)

        return Signal(Action.WAIT, score, f"Filters failed: {', '.join(failed)}", price, atr)


class MeanReversionStrategy:
    """Buy oversold stretches, exit on reversion.

    Backtested as the best single-signal variant on XRP/USD (lowest fee bleed,
    ~0.5% max drawdown over 120d). Entry: RSI washed out (< ``rsi_oversold``)
    AND price trading below the slow trend EMA (a stretch, not a free-fall).
    Exit: RSI recovers above ``rsi_exit`` OR price reclaims the slow EMA.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def evaluate(self, bars: pd.DataFrame, in_position: bool = False) -> Signal:
        s = self.settings
        frame = enrich(
            bars,
            fast_ema=s.fast_ema,
            slow_ema=s.slow_ema,
            regime_ema=s.regime_ema,
            rsi_period=s.rsi_period,
            atr_period=s.atr_period,
            breakout_lookback=s.breakout_lookback,
            volume_lookback=s.volume_lookback,
        ).dropna()
        if frame.empty:
            return Signal(Action.WAIT, 0, "Not enough completed bars", 0.0)
        row = frame.iloc[-1]
        price = float(row["close"])
        atr = max(float(row["atr"]), price * 1e-4)
        if not math.isfinite(atr) or atr <= 0:
            return Signal(Action.WAIT, 0, "ATR unavailable", price)

        oversold = s.rsi_oversold
        exit_rsi = s.rsi_exit
        slow = float(row["ema_slow"])

        if in_position:
            if float(row["rsi"]) >= exit_rsi or price >= slow:
                return Signal(
                    Action.SELL, 80,
                    f"Mean reversion complete (rsi={float(row['rsi']):.0f} "
                    f"{'recovered' if float(row['rsi']) >= exit_rsi else 'reclaimed EMA'})",
                    price, atr,
                )
            return Signal(Action.WAIT, 55, "Holding reversion", price, atr)

        if float(row["rsi"]) <= oversold and price < slow:
            stop_price = max(0.0, price - atr * s.atr_stop_multiplier)
            note = f"Oversold reversion (rsi={float(row['rsi']):.0f}, below slow EMA)"
            return Signal(Action.BUY, 60, note, price, atr, stop_price)
        return Signal(
            Action.WAIT, 10,
            f"Not oversold (rsi={float(row['rsi']):.0f}, oversold<={oversold:.0f})",
            price, atr,
        )


def build_strategy(settings: Settings):
    """Factory: pick the active strategy from config."""
    name = getattr(settings, "strategy", "momentum")
    if name == "mean_reversion":
        return MeanReversionStrategy(settings)
    return TrendBreakoutStrategy(settings)



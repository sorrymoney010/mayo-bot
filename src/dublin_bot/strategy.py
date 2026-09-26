from __future__ import annotations

import math
import pandas as pd

from .config import Settings
from .indicators import adx_trend_allowed, enrich, fee_edge_ok
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
            adx_period=getattr(s, "adx_period", 14),
        ).dropna(subset=["ema_fast", "ema_slow", "ema_regime", "rsi", "atr"])
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

        # HARD ADX sit-out: momentum only when a trend is present.
        # ADX > enter_above → allow; ADX < exit_below → chop sit-out;
        # between bands → hysteresis (walk series for restart-safe state).
        if getattr(s, "adx_gate_enabled", True):
            adx_series = frame["adx"].dropna()
            allowed = adx_trend_allowed(
                adx_series.tolist(),
                enter_above=float(getattr(s, "adx_enter_above", 25.0)),
                exit_below=float(getattr(s, "adx_exit_below", 20.0)),
                initial=False,
            )
            adx_now = float(row["adx"]) if "adx" in row and math.isfinite(float(row["adx"])) else float("nan")
            if not allowed:
                adx_txt = f"{adx_now:.1f}" if math.isfinite(adx_now) else "n/a"
                return Signal(
                    Action.WAIT,
                    15,
                    f"ADX sit-out: chop (adx={adx_txt})",
                    price,
                    atr,
                )

        # Fee-aware min edge: TP distance must clear configured bps before fees.
        if getattr(s, "min_edge_gate_enabled", True):
            ok, reason = fee_edge_ok(
                float(getattr(s, "take_profit_pct", 0.0)),
                float(getattr(s, "min_edge_bps", 100.0)),
            )
            if not ok:
                return Signal(Action.WAIT, 10, reason, price, atr)

        checks = {
            # Momentum is the hard gate: RSI must sit inside the tradable band
            # (not overbought, not washed out).
            "momentum": s.rsi_min <= float(row["rsi"]) <= s.rsi_max,
        }
        # Regime is a HARD gate for entries (no counter-trend BUYs).
        # Trend / breakout / volume remain advisory for scoring/notes only.
        advisory = {
            "regime": price > float(row["ema_regime"]),
            "trend": float(row["ema_fast"]) > float(row["ema_slow"]),
            "breakout": price >= float(row["prior_resistance"]) - atr,
            "volume": float(row["volume_ratio"]) >= s.min_volume_ratio,
        }
        score = (sum(checks.values()) * 20) + (sum(advisory.values()) * 5)
        failed = [name for name, passed in checks.items() if not passed]

        if not failed:
            # Hard regime gate: momentum alone is not enough below EMA regime.
            if not advisory["regime"]:
                return Signal(
                    Action.WAIT,
                    score,
                    "Momentum blocked: counter-trend",
                    price,
                    atr,
                )
            stop_price = max(0.0, price - atr * s.atr_stop_multiplier)
            note = "Momentum confirmed"
            if advisory["breakout"]:
                note += " + at resistance"
            if not advisory["volume"]:
                note += "; low volume (advisory)"
            if "adx" in row and math.isfinite(float(row["adx"])):
                note += f"; adx={float(row['adx']):.1f}"
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
            adx_period=getattr(s, "adx_period", 14),
        ).dropna(subset=["ema_fast", "ema_slow", "ema_regime", "rsi", "atr"])
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
    """Factory: pick the active strategy from config.

    Supported names:
    - ``momentum`` (default): RSI band + hard regime EMA + ADX sit-out + fee min-edge
    - ``mean_reversion``: oversold stretch reversion
    - ``sr_flip``: support/resistance flip reclaim
    - ``pattern`` / ``elliott_lite``: rule-based OHLC patterns (double bottom / flag)
    - ``breakout`` / ``momentum_breakout``: N-bar high + volume breakout (defined-risk)
    - ``regime_trend``: ADX/vol-gated trend follower, flat in chop (walk-forward pick)
    """
    name = getattr(settings, "strategy", "momentum")
    if name == "mean_reversion":
        return MeanReversionStrategy(settings)
    if name == "sr_flip":
        from .strategies.sr_flip_strategy import SRFlipStrategy
        return SRFlipStrategy(settings)
    if name in ("pattern", "elliott_lite"):
        from .strategies.pattern_strategy import PatternStrategy
        return PatternStrategy(settings)
    if name in ("breakout", "momentum_breakout"):
        from .strategies.breakout_strategy import BreakoutStrategy
        return BreakoutStrategy(settings)
    if name in ("regime_trend", "regime"):
        from .strategies.regime_strategy import RegimeTrendStrategy
        return RegimeTrendStrategy(settings)
    return TrendBreakoutStrategy(settings)

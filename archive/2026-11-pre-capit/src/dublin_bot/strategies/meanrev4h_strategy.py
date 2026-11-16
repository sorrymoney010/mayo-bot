"""4h mean-reversion signal (second PAPER sleeve, see ``meanrev_sleeve``).

Rules (evaluated on the last CLOSED bar, identical to the walk-forward
family ``meanrev_mk`` via ``backtest_core.meanrev_signals``):

Entry signal:  RSI(14) <= MEANREV_RSI_ENTRY (38) AND close < EMA50.
               The sleeve then rests a post-only limit buy
               MEANREV_LIMIT_OFFSET_PCT (0.1%) under that close, valid for
               MEANREV_LIMIT_VALID_BARS (1) bar. Unfilled → expires.
Exit signal:   RSI(14) >= MEANREV_RSI_EXIT (55) OR close >= EMA50
               ("reverted") on a bar that closed after the fill.
Protective:    stop MEANREV_STOP_PCT (3%) below the fill, take-profit
               MEANREV_TAKE_PROFIT_PCT (25%) above it — both handled by the
               sleeve from the live ticker.
"""
from __future__ import annotations

import math

import pandas as pd

from dublin_bot.backtest_core import add_indicators, meanrev_signals, regime_label, adx_hysteresis
from dublin_bot.config import Settings
from dublin_bot.models import Action, Signal

MIN_BARS = 210  # same warm-up the backtester uses before the first trade


class MeanReversion4hStrategy:
    name = "meanrev_4h"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def params(self) -> dict:
        s = self.settings
        return {
            "rsi_os": float(s.meanrev_rsi_entry),
            "rsi_exit": float(s.meanrev_rsi_exit),
            "ema": int(s.meanrev_ema_period),
            "stop": float(s.meanrev_stop_pct),
            "tp": float(s.meanrev_take_profit_pct),
            "limit_offset": float(s.meanrev_limit_offset_pct),
            "flt": str(getattr(s, "meanrev_flow_filter", "") or ""),
            "d1": bool(getattr(s, "meanrev_daily_filter", True)),
        }

    def enrich(self, bars: pd.DataFrame) -> pd.DataFrame:
        return add_indicators(bars.sort_index())

    def regime(self, d: pd.DataFrame) -> str:
        s = self.settings
        on = adx_hysteresis(d["adx"], float(s.adx_enter_above), float(s.adx_exit_below)).to_numpy()
        return regime_label(bool(on[-1]), float(d["atr_rank"].iloc[-1]))

    def evaluate(self, bars: pd.DataFrame | None, in_position: bool = False) -> Signal:
        self.d1_blocked = False
        self.last_frame = None
        n = 0 if bars is None else len(bars)
        if bars is None or n < MIN_BARS:
            return Signal(Action.WAIT, 0, f"Not enough bars for meanrev sleeve ({n} < {MIN_BARS})", 0.0)
        d = self.enrich(bars)
        p = self.params()
        if p["d1"] and not in_position:  # D1 gates entries only, never exits
            from dublin_bot.daily_filter import live_d1
            d = live_d1(self, d, int(self.settings.timeframe_minutes))
        else:
            p = {**p, "d1": False}
        sig = meanrev_signals(d, p)
        self.last_frame = d
        i = len(d) - 1
        # the raw rules fired but the daily filter stopped it -> shadow-trade candidate
        self.d1_blocked = bool(p["d1"] and not sig["d1_ok"][i]
                               and meanrev_signals(d, {**p, "d1": False})["entry"][i])
        price = float(d["close"].iloc[i])
        rsi = float(sig["rsi"][i])
        ema = float(sig["ema"][i])
        atr = float(d["atr"].iloc[i])
        atr = atr if math.isfinite(atr) and atr > 0 else None
        txt = f"rsi={rsi:.1f} close={price:.6g} ema{p['ema']}={ema:.6g}"
        if in_position:
            if sig["exit"][i]:
                return Signal(Action.SELL, 80, f"Meanrev exit (reverted): {txt}", price, atr)
            return Signal(Action.WAIT, 50, f"Holding meanrev: {txt}", price, atr)
        if sig["entry"][i]:
            limit = price * (1.0 - p["limit_offset"])
            stop = limit * (1.0 - p["stop"])
            return Signal(Action.BUY, 75,
                          f"Meanrev entry: rsi {rsi:.1f} <= {p['rsi_os']:g} and close < "
                          f"ema{p['ema']} ({txt})", limit, atr, stop)
        why = []
        if not rsi <= p["rsi_os"]:
            why.append(f"rsi {rsi:.1f} > {p['rsi_os']:g}")
        if not price < ema:
            why.append(f"close >= ema{p['ema']}")
        if p["flt"] and not sig["flt_ok"][i]:
            why.append(f"order-flow filter {p['flt']} not met")
        if p["d1"] and not sig["d1_ok"][i]:
            from dublin_bot.daily_filter import d1_reason
            why.append(d1_reason(d, i))
        return Signal(Action.WAIT, 10, "No meanrev entry: " + "; ".join(why) + f" ({txt})", price, atr)

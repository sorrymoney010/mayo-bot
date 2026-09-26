"""Regime-switch trend sleeve (STRATEGY=regime_trend).

Trades ONLY when the ADX regime says "trend" and sits flat in chop — the
variant that walk-forward tested least-bad/best after Kraken taker fees.

Entry (on the last CLOSED bar):
  ADX hysteresis on (ADX > enter, stays on until ADX < exit)
  AND optional ATR% expansion (rank >= regime_min_atr_rank)
  AND close > EMA200 AND EMA20 > EMA50
  AND close > prior N-bar high (N = regime_lookback)
Exit (in position):
  close < 22-bar highest high - regime_atr_mult * ATR   (stateless chandelier)
  OR ADX regime flips to chop (ADX < exit)
Hard stop: engine paper protective stop at STOP_LOSS_PCT. TAKE_PROFIT_PCT is
set wide (e.g. 25%) so winners run; fee min-edge gate stays satisfied.

The exact same rules are replayed by ``backtest_core.simulate`` (family
"regime") — see ``regime_signals`` which both use.
"""
from __future__ import annotations

import math

import pandas as pd

from dublin_bot.backtest_core import add_indicators, regime_label, regime_signals
from dublin_bot.config import Settings
from dublin_bot.models import Action, Signal


class RegimeTrendStrategy:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def params(self) -> dict:
        s = self.settings
        return {
            "lookback": int(getattr(s, "regime_lookback", 20)),
            "atr_mult": float(getattr(s, "regime_atr_mult", 3.0)),
            "min_atr_rank": float(getattr(s, "regime_min_atr_rank", 0.0)),
            "adx_enter": float(getattr(s, "adx_enter_above", 25.0)),
            "adx_exit": float(getattr(s, "adx_exit_below", 20.0)),
            "stop": float(getattr(s, "stop_loss_pct", 0.03)),
        }

    def evaluate(self, bars: pd.DataFrame, in_position: bool = False) -> Signal:
        if bars is None or len(bars) < 260:
            n = 0 if bars is None else len(bars)
            return Signal(Action.WAIT, 0, f"Not enough bars for regime sleeve ({n} < 260)", 0.0)
        d = add_indicators(bars.sort_index())
        p = self.params()
        sig = regime_signals(d, p)
        i = len(d) - 1
        price = float(d["close"].iloc[i])
        atr = float(d["atr"].iloc[i])
        atr = atr if math.isfinite(atr) and atr > 0 else None
        adx = float(d["adx"].iloc[i])
        reg = regime_label(bool(sig["adx_on"][i]), float(d["atr_rank"].iloc[i]))
        adx_txt = f"{adx:.1f}" if math.isfinite(adx) else "n/a"

        if in_position:
            if sig["chandelier_exit"][i]:
                return Signal(Action.SELL, 85,
                              f"Regime exit: close {price:.6g} < chandelier "
                              f"{sig['chandelier'][i]:.6g} (22-bar HH - {p['atr_mult']:g}xATR)",
                              price, atr)
            if not sig["adx_on"][i]:
                return Signal(Action.SELL, 80, f"Regime exit: ADX {adx_txt} → chop, go flat",
                              price, atr)
            return Signal(Action.WAIT, 60,
                          f"Holding trend (adx={adx_txt}, chandelier={sig['chandelier'][i]:.6g})",
                          price, atr)

        if sig["entry"][i]:
            stop = price * (1.0 - p["stop"])
            return Signal(Action.BUY, 80,
                          f"Regime trend entry [{reg}]: adx={adx_txt}, close {price:.6g} > "
                          f"{p['lookback']}-bar high {sig['prior_high'][i]:.6g}, EMA20>EMA50>…, >EMA200",
                          price, atr, stop)
        why = []
        if not sig["adx_on"][i]:
            why.append(f"chop (adx={adx_txt}) — sitting flat")
        else:
            if not sig["vol_ok"][i]:
                why.append("ATR% below expansion rank")
            if not (price > float(d["ema200"].iloc[i])):
                why.append("below EMA200")
            if not (float(d["ema20"].iloc[i]) > float(d["ema50"].iloc[i])):
                why.append("EMA20<=EMA50")
            if not (price > sig["prior_high"][i]):
                why.append(f"no {p['lookback']}-bar high break")
        return Signal(Action.WAIT, 10, f"No regime entry [{reg}]: " + "; ".join(why), price, atr)

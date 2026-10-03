"""Walk-forward for the paper perpetual SHORT sleeve. Offline, no orders.

Entry is the bearish mirror of trend-hold, on bars of 1h or slower:

* daily filter known and OFF (close below a rising-SMA50 test — the opposite
  of ``daily_filter.riskon``)
* close < EMA(slow) and EMA(fast) < EMA(slow)
* fill at the next bar's open

Exit at the first close back above EMA(slow), or at the isolated liquidation
price, whichever the bar hits first. Funding is applied once per bar from a
caller-supplied hourly-rate series (summed into the bar). Costs are the
published futures taker schedule, not the spot 40/25 model.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .backtest_core import Costs, Metrics, Trade, add_indicators, metrics
from .daily_filter import d1_mask
from .futures_costs import (
    FUTURES_TAKER_FEE_BPS,
    clamp_leverage,
    fee_usd,
    liquidation_price,
)
from .technicals import ema


def futures_costs() -> Costs:
    """Taker/taker futures fills. Slippage stays 0 here; the sleeve adds none
    beyond the published fee (mark/last is the paper print)."""
    return Costs(fee_bps=FUTURES_TAKER_FEE_BPS, slippage_bps=0.0, maker_bps=FUTURES_TAKER_FEE_BPS)


def bearish_entry_mask(d: pd.DataFrame, *, ema_fast: int = 20, ema_slow: int = 100) -> np.ndarray:
    ef = (d["ema20"] if ema_fast == 20 and "ema20" in d else ema(d["close"], ema_fast)).to_numpy(float)
    es = ema(d["close"], ema_slow).to_numpy(float)
    close = d["close"].to_numpy(float)
    warm = np.arange(len(d)) >= ema_slow
    # d1_mask is the risk-on (bullish) gate. Bearish entries require it to be
    # known and false. Missing d1 fails closed (no short).
    if "d1_riskon" not in d:
        bear = np.zeros(len(d), dtype=bool)
    else:
        known = np.isfinite(d["d1_riskon"].to_numpy(float))
        bear = known & ~d1_mask(d)
    entry = warm & (close < es) & (ef < es) & bear
    return entry


@dataclass
class ShortSimParams:
    ema_fast: int = 20
    ema_slow: int = 100
    leverage: float = 1.0
    maintenance: float = 0.01
    warm: int = 210
    # Optional trailing overlay (ATR at entry). 0 disables it.
    trail_activate_atr: float = 0.0
    trail_atr: float = 1.0


def simulate_perp_short(d: pd.DataFrame, params: ShortSimParams, *,
                        funding_hourly: np.ndarray | None = None,
                        bar_hours: float = 1.0) -> list[Trade]:
    """Replay one short sleeve. ``funding_hourly`` aligns with bars (the rate
    that will be charged for one hour at that bar). ``bar_hours`` scales it.
    """
    from .trailing import TrailState, on_bar

    if "ema20" not in d.columns:
        d = add_indicators(d)
    n = len(d)
    o = d["open"].to_numpy(float)
    h = d["high"].to_numpy(float)
    l = d["low"].to_numpy(float)  # noqa: E741
    c = d["close"].to_numpy(float)
    t = d["time"].to_numpy()
    atr = d["atr"].to_numpy(float) if "atr" in d else np.full(n, np.nan)
    es = ema(d["close"], params.ema_slow).to_numpy(float)
    entry_m = bearish_entry_mask(d, ema_fast=params.ema_fast, ema_slow=params.ema_slow)
    lev = clamp_leverage(params.leverage)
    rates = np.zeros(n) if funding_hourly is None else np.asarray(funding_hourly, float)
    if len(rates) != n:
        rates = np.resize(rates, n)

    trades: list[Trade] = []
    i = int(params.warm)
    while i < n - 1:
        if not entry_m[i] or not math.isfinite(c[i]):
            i += 1
            continue
        e = i + 1
        entry = float(o[e])
        if not math.isfinite(entry) or entry <= 0:
            i += 1
            continue
        liq = liquidation_price(entry, side="short", leverage=lev, maintenance=params.maintenance)
        # stop_frac feeds the shared sizer: distance to liquidation.
        stop_frac = max(liq / entry - 1.0, 1e-6)
        atr_e = float(atr[i]) if math.isfinite(atr[i]) and atr[i] > 0 else 0.0
        trail = None
        if params.trail_activate_atr > 0 and atr_e > 0:
            trail = TrailState(
                sleeve="futures_short", symbol="", side="short", entry=entry, atr=atr_e,
                activate_atr=params.trail_activate_atr, trail_atr=params.trail_atr, peak=entry,
            )
        exit_px = None
        reason = ""
        funding_ret = 0.0
        j = e
        while j < n:
            # Liquidation first (gap, then wick). A short dies on a rally.
            if o[j] >= liq and j > e:
                exit_px, reason = float(o[j]), "liquidation_gap"
            elif h[j] >= liq:
                exit_px, reason = float(liq), "liquidation"
            if exit_px is None and trail is not None:
                trail, tr_reason, tr_px = on_bar(trail, open_=float(o[j]), high=float(h[j]), low=float(l[j]))
                if tr_reason:
                    exit_px, reason = float(tr_px), tr_reason
            if exit_px is not None:
                break
            # Funding for the hours the position was open during this bar.
            # Positive Kraken funding: longs pay shorts, so a short's return rises.
            funding_ret += float(rates[j]) * bar_hours
            if c[j] > es[j] and j + 1 < n:
                j += 1
                exit_px, reason = float(o[j]), "above_ema"
                break
            j += 1
        if exit_px is None:
            j = n - 1
            exit_px, reason = float(c[j]), "eod_mark"
        # Price return of the short, minus two taker fees, plus funding.
        gross = (entry - exit_px) / entry
        fee = (FUTURES_TAKER_FEE_BPS / 1e4) * 2.0
        net = gross - fee + funding_ret
        trades.append(Trade(
            entry_i=e, exit_i=j, entry_time=int(t[e]), exit_time=int(t[j]),
            entry_px=entry, exit_px=float(exit_px), stop_frac=float(stop_frac),
            gross=float(gross), net=float(net), reason=reason, regime="short",
        ))
        i = j + 1
    return trades


def short_metrics(trades: list[Trade], *, equity: float = 500.0, leverage: float = 1.0) -> Metrics:
    """Size each trade with isolated margin = min(25% equity, exposure room) and
    notional = margin * leverage, capped so notional stays inside a 75% book.
    """
    lev = clamp_leverage(leverage)
    # Reuse the shared metrics sizer but the stop_frac is already the liq distance,
    # which at 2x is ~49% and would undersize. Override by rewriting stop_frac so
    # notional ~= equity * 0.25 * lev clipped to 0.75 * equity.
    target_frac = min(0.25 * lev, 0.75)
    adjusted = []
    for tr in trades:
        # metrics() does notional = min(eq * risk / stop_frac, eq * max_pos_frac)
        # with risk=0.01 and max_pos_frac=target. Set stop_frac so risk/stop == target
        # when that is the binding cap: stop_frac = risk / target.
        adjusted.append(Trade(
            entry_i=tr.entry_i, exit_i=tr.exit_i, entry_time=tr.entry_time, exit_time=tr.exit_time,
            entry_px=tr.entry_px, exit_px=tr.exit_px, stop_frac=0.01 / target_frac,
            gross=tr.gross, net=tr.net, reason=tr.reason, regime=tr.regime,
        ))
    return metrics(adjusted, equity=equity, risk=0.01, max_pos_frac=target_frac)


def position_funding_usd(*, side: str, notional: float, hourly_rate: float, hours: float) -> float:
    """Convenience wrapper kept next to the simulator for the live sleeve."""
    from .futures_costs import funding_cashflow
    return funding_cashflow(side=side, notional=notional, hourly_rate=hourly_rate, hours=hours)


def round_trip_fee_usd(notional: float) -> float:
    return fee_usd(notional) * 2.0

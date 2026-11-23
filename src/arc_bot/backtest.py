"""Event-driven 1m-bar backtest of ORB under PropGuard; plus eval-pass Monte Carlo."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .rules import Instrument, PropGuard, PropRules
from .strategy import ORBParams, vwap


@dataclass
class Trade:
    day: str
    side: int
    entry: float
    exit: float
    qty: int
    pnl: float
    reason: str


@dataclass
class Result:
    trades: list[Trade] = field(default_factory=list)
    day_pnls: list[float] = field(default_factory=list)
    guard: PropGuard | None = None


def _hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def run(bars: pd.DataFrame, inst: Instrument, p: ORBParams = ORBParams(),
        rules: PropRules = PropRules(), slippage_ticks: float = 1.0,
        stop_on_pass: bool = False) -> Result:
    guard = PropGuard(rules)
    res = Result(guard=guard)
    slip = slippage_ticks * inst.tick
    days = [g for _, g in bars.groupby(bars.index.date)]
    daily_ranges: list[float] = []
    ch, cm = _hm(p.entry_cutoff)
    fh, fm = _hm(p.flatten_at)

    for g in days:
        if guard.blown or (stop_on_pass and guard.passed()):
            break
        day = str(g.index[0].date())
        avg_rng = float(np.mean(daily_ranges[-10:])) if len(daily_ranges) >= 5 else None
        daily_ranges.append(float(g["high"].max() - g["low"].min()))
        orb = g.iloc[: p.or_minutes]
        rest = g.iloc[p.or_minutes:]
        if len(orb) < p.or_minutes or avg_rng is None:
            guard.end_day(); continue
        hi, lo = float(orb["high"].max()), float(orb["low"].min())
        rng = hi - lo
        if not (p.min_range_frac <= rng / avg_rng <= p.max_range_frac):
            guard.end_day(); continue
        vw = vwap(g)
        mid = (hi + lo) / 2
        pos = None  # dict(side, entry, stop, target, qty, risk_pts)
        trades_today = 0
        pending = 0  # side to enter at next bar open
        for i, (ts, b) in enumerate(rest.iterrows()):
            t = (ts.hour, ts.minute)
            if pending and pos is None:
                side = pending; pending = 0
                entry = b["open"] + side * slip
                stop = mid if p.stop_mode == "mid" else (lo if side > 0 else hi)
                risk_pts = abs(entry - stop)
                budget = guard.max_risk_now(p.risk_per_trade)
                if 0 < risk_pts <= p.max_stop_pts and budget > 0:
                    qty = int(budget // (risk_pts * inst.point_value))
                    if guard.contracts_ok(qty):
                        pos = dict(side=side, entry=entry, stop=stop, qty=qty, risk=risk_pts,
                                   target=entry + side * p.target_r * risk_pts, be=False)
                        trades_today += 1
            if pos:
                s = pos["side"]
                adverse = b["low"] if s > 0 else b["high"]
                fav = b["high"] if s > 0 else b["low"]
                open_pnl = (adverse - pos["entry"]) * s * pos["qty"] * inst.point_value
                guard.mark_equity(open_pnl)
                exit_px, reason = None, ""
                if guard.blown:
                    exit_px, reason = adverse, "BLOWN"
                elif (adverse - pos["stop"]) * s <= 0:
                    exit_px, reason = pos["stop"] - s * slip, "stop"   # stop first (conservative)
                elif (fav - pos["target"]) * s >= 0:
                    exit_px, reason = pos["target"], "target"
                elif t >= (fh, fm):
                    exit_px, reason = b["close"] - s * slip, "eod"
                if exit_px is None and p.be_after_r and not pos["be"] \
                        and (fav - pos["entry"]) * s >= p.be_after_r * pos["risk"]:
                    pos["stop"], pos["be"] = pos["entry"], True
                if exit_px is not None:
                    gross = (exit_px - pos["entry"]) * s * pos["qty"] * inst.point_value
                    pnl = gross - 2 * inst.fee_per_side * pos["qty"]
                    guard.realize(pnl)
                    res.trades.append(Trade(day, s, pos["entry"], exit_px, pos["qty"], pnl, reason))
                    pos = None
                    if guard.blown:
                        break
                continue
            # flat: look for signal on bar close
            if (trades_today < p.max_trades_per_day and guard.can_trade() and t < (ch, cm)):
                c = b["close"]
                v = vw.loc[ts]
                if c > hi + p.entry_buffer_pts and (not p.use_vwap_filter or c > v):
                    pending = 1
                elif c < lo - p.entry_buffer_pts and (not p.use_vwap_filter or c < v):
                    pending = -1
        if pos:  # safety: data ended before flatten
            last = rest.iloc[-1]["close"]
            pnl = (last - pos["entry"]) * pos["side"] * pos["qty"] * inst.point_value \
                - 2 * inst.fee_per_side * pos["qty"]
            guard.realize(pnl)
            res.trades.append(Trade(day, pos["side"], pos["entry"], last, pos["qty"], pnl, "data_end"))
        res.day_pnls.append(guard.end_day())
    return res


def stats(res: Result) -> dict:
    t = res.trades
    pnl = np.array([x.pnl for x in t]) if t else np.array([0.0])
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    dp = np.array(res.day_pnls) if res.day_pnls else np.array([0.0])
    g = res.guard
    return dict(
        trades=len(t), net=round(float(pnl.sum()), 2),
        win_rate=round(float((pnl > 0).mean()), 3),
        profit_factor=round(float(wins.sum() / -losses.sum()), 2) if losses.sum() < 0 else math.inf,
        expectancy=round(float(pnl.mean()), 2),
        best_day=round(float(dp.max()), 2), worst_day=round(float(dp.min()), 2),
        blown=g.blown, passed=g.passed(), qualifying_days=g.qualifying_days(),
        consistency_ok=g.consistency_ok(),
    )


def monte_carlo_pass(day_pnls: list[float], rules: PropRules = PropRules(), n: int = 5000,
                     max_days: int = 60, seed: int = 1, guard_kw: dict | None = None) -> dict:
    """Bootstrap daily P&Ls into eval attempts. Honest answer to 'what are my odds?'.

    Only as good as day_pnls: feed it out-of-sample real-data results, never in-sample tuning.
    """
    rng = np.random.default_rng(seed)
    d = np.array(day_pnls)
    if d.size < 20:
        return {"error": "need >=20 daily samples"}
    passes = blows = 0
    days_to_pass = []
    for _ in range(n):
        g = PropGuard(rules, **(guard_kw or {}))
        for k in range(max_days):
            x = float(rng.choice(d))
            # conservative intraday check: a losing day is assumed to touch its close
            g.mark_equity(min(x, 0.0))
            if g.blown:
                blows += 1; break
            g.realize(x); g.end_day()
            if g.passed():
                passes += 1; days_to_pass.append(k + 1); break
    return dict(p_pass=passes / n, p_blow=blows / n,
                median_days=float(np.median(days_to_pass)) if days_to_pass else None)

"""Bar-replay with order-flow ENTRY FILTERS and EXIT TIMERS on a 15m execution grid.

Entries are the existing sleeves' rules on CLOSED 1h/4h bars (``backtest_core``
``regime_signals`` / ``meanrev_signals`` / ``trendhold_signals``), filled at the next
tf bar's open exactly like ``backtest_core.simulate``. Order flow only ever:

* removes an entry (``entry_filter``), or
* changes how a position is exited, evaluated on 15m bars inside the position:
  - ``climax``/``euphoria``: exit (``frac`` 1.0) or scale out (``frac`` 0.5) at the next
    15m open, taker, once the position is up >= ``min_gain`` and the trigger fires
    (sell into strength / when big players cash out);
  - ``shakeout``: the protective stop becomes a 15m-CLOSE stop that is ignored while the
    dip shows absorption (``dip_absorbed``), with a hard intrabar stop at ``hard_mult`` x
    the stop distance; chandelier / EMA signal exits are deferred up to 2 tf bars while
    sellers are being absorbed;
  - ``closestop``: the same close-based stop + hard stop WITHOUT the absorption logic
    (control, isolates the order-flow contribution);
  - ``ptp``: 50% resting take-profit (maker) at entry + ``ptp_atr`` x ATR.

Costs come from ``backtest_core.Costs``. Stops fill first when a bar touches both.
No network, no orders, no locks.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .backtest_core import Costs, Spec, adx_hysteresis, meanrev_signals, regime_signals, trendhold_signals
from .orderflow import entry_filter_mask


@dataclass(frozen=True)
class Overlay:
    name: str = "baseline"
    entry_filter: str | None = None
    exit_trigger: str | None = None      # "buy_climax" | "euphoria"
    exit_frac: float = 1.0
    min_gain: float = 0.015
    stop_mode: str = "intrabar"          # "intrabar" | "closestop" | "shakeout"
    hard_mult: float = 2.0
    ptp_atr: float = 0.0
    max_defer: int = 2


@dataclass
class FTrade:
    entry_i: int
    exit_i: int
    entry_time: int
    exit_time: int
    entry_px: float
    exit_px: float          # weighted exit price
    stop_frac: float
    gross: float
    net: float
    reason: str
    legs: list = field(default_factory=list)


def _signals(d: pd.DataFrame, spec: Spec) -> dict:
    p = spec.params
    if spec.name == "regime":
        s = regime_signals(d, p)
        adx_on = s["adx_on"]
        ex = np.asarray(s["chandelier_exit"], dtype=bool)
        chop = (~np.asarray(adx_on, dtype=bool)) if p.get("exit_on_chop", True) else np.zeros(len(d), bool)
        return {"entry": np.asarray(s["entry"], bool), "exit_soft": ex, "exit_chop": chop,
                "exit_profit": np.zeros(len(d), bool)}
    if spec.name == "meanrev":
        s = meanrev_signals(d, p)
        z = np.zeros(len(d), bool)
        return {"entry": np.asarray(s["entry"], bool), "exit_soft": z, "exit_chop": z,
                "exit_profit": np.asarray(s["exit"], bool)}
    if spec.name == "trendhold":
        s = trendhold_signals(d, p)
        z = np.zeros(len(d), bool)
        return {"entry": np.asarray(s["entry"], bool), "exit_soft": np.asarray(s["exit"], bool),
                "exit_chop": z, "exit_profit": z}
    raise ValueError(spec.name)


def simulate_flow(d: pd.DataFrame, fb: pd.DataFrame, spec: Spec, costs: Costs,
                  ov: Overlay = Overlay(), *, tf_minutes: int, warm: int = 210) -> list[FTrade]:
    """``d``: indicator-enriched tf frame with ``time`` and the tf flow columns.
    ``fb``: 15m frame from ``orderflow.fine_features`` covering the same span."""
    p = spec.params
    sig = _signals(d, spec)
    entry_ok = sig["entry"] & entry_filter_mask(d, ov.entry_filter)
    n = len(d)
    t_tf = d["time"].to_numpy(np.int64)
    o_tf, l_tf, c_tf = (d[k].to_numpy(float) for k in ("open", "low", "close"))
    atr = d["atr"].to_numpy(float)
    ft = fb["time"].to_numpy(np.int64)
    fo, fh, fl, fc = (fb[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    nf = len(fb)
    pos_of = {int(x): i for i, x in enumerate(ft)}
    tfs = tf_minutes * 60
    trig = fb[ov.exit_trigger].to_numpy(bool) if ov.exit_trigger else np.zeros(nf, bool)
    absorbed = fb["dip_absorbed"].to_numpy(bool) if "dip_absorbed" in fb else np.zeros(nf, bool)
    tf_absorbed = (d["bull_abs_60"].fillna(False).to_numpy(bool) | d["sell_climax_4h"].fillna(False).to_numpy(bool)) \
        if "bull_abs_60" in d else np.zeros(n, bool)
    tf_pos = {int(x): i for i, x in enumerate(t_tf)}
    entry_maker = p.get("entry", "market") == "limit"
    trades: list[FTrade] = []
    i = warm
    fsec = FINE_SECONDS(fb)
    while i < n - 1:
        if not entry_ok[i] or not math.isfinite(atr[i]):
            i += 1
            continue
        e = i + 1
        if t_tf[e] != t_tf[i] + tfs or int(t_tf[e]) not in pos_of:
            i += 1
            continue
        fe = pos_of[int(t_tf[e])]
        if entry_maker:
            limit = c_tf[i] * (1 - p.get("limit_offset", 0.001))
            if l_tf[e] > limit:
                i += 1
                continue
            entry = min(o_tf[e], limit)
            j0 = fe
            while j0 < nf and fl[j0] > limit:
                j0 += 1
            if j0 >= nf:
                break
        else:
            entry = o_tf[e]
            j0 = fe
        if spec.name == "trendhold":
            stop_frac = float(p.get("stop", 0.0)) or 0.25
            stop = entry * (1 - float(p["stop"])) if p.get("stop") else -math.inf
            tp = entry * (1 + float(p["tp"])) if p.get("tp") else math.inf
        else:
            stop_frac = float(p.get("stop", 0.03))
            stop = entry * (1 - stop_frac)
            tp = entry * (1 + float(p.get("tp", 0.25)))
        hard = entry * (1 - ov.hard_mult * stop_frac) if math.isfinite(stop) else -math.inf
        if spec.name == "trendhold" and ov.stop_mode in ("shakeout", "closestop"):
            hard = -math.inf
        ptp = entry + ov.ptp_atr * atr[i] if ov.ptp_atr > 0 else math.inf
        legs: list[tuple[float, float, bool, str]] = []   # (frac, px, maker, reason)
        remaining = 1.0
        partial_done = False
        defers = 0
        exit_j = None
        j = j0
        pending: tuple[str, float] | None = None   # exit at next fine open: (reason, frac)
        while j < nf:
            if pending is not None:
                reason, frac = pending
                pending = None
                take = min(frac, remaining)
                legs.append((take, fo[j], False, reason))
                remaining -= take
                if remaining <= 1e-9:
                    exit_j = j
                    break
            # protective stop
            if ov.stop_mode == "intrabar":
                if fo[j] <= stop and j > j0:
                    legs.append((remaining, fo[j], False, "stop_gap")); remaining = 0; exit_j = j; break
                if fl[j] <= stop:
                    legs.append((remaining, stop, False, "stop")); remaining = 0; exit_j = j; break
            else:
                if fo[j] <= hard and j > j0:
                    legs.append((remaining, fo[j], False, "hard_stop_gap")); remaining = 0; exit_j = j; break
                if fl[j] <= hard:
                    legs.append((remaining, hard, False, "hard_stop")); remaining = 0; exit_j = j; break
            # take profits (maker at the level, taker on a gap)
            if fo[j] >= tp and j > j0:
                legs.append((remaining, fo[j], False, "tp_gap")); remaining = 0; exit_j = j; break
            if fh[j] >= tp:
                legs.append((remaining, tp, True, "tp")); remaining = 0; exit_j = j; break
            if not partial_done and fh[j] >= ptp:
                take = 0.5 * remaining
                px = fo[j] if (fo[j] >= ptp and j > j0) else ptp
                legs.append((take, px, not (fo[j] >= ptp and j > j0), "ptp"))
                remaining -= take
                partial_done = True
            # close-based soft stop
            if ov.stop_mode in ("closestop", "shakeout") and math.isfinite(stop) and fc[j] <= stop:
                if not (ov.stop_mode == "shakeout" and absorbed[j]):
                    pending = ("soft_stop", 1.0)
            # flow exit timer: sell into strength
            if pending is None and trig[j] and fc[j] / entry - 1 >= ov.min_gain:
                if ov.exit_frac >= 1.0:
                    pending = (f"flow_{ov.exit_trigger}", 1.0)
                elif not partial_done:
                    pending = (f"flow_{ov.exit_trigger}_half", ov.exit_frac)
                    partial_done = True
            # tf-close signal exits
            if pending is None and ((ft[j] + fsec) % tfs) == 0:
                k = tf_pos.get(int((ft[j] // tfs) * tfs))
                if k is not None:
                    if sig["exit_profit"][k]:
                        pending = ("reverted", 1.0)
                    elif sig["exit_soft"][k] or sig["exit_chop"][k]:
                        if (ov.stop_mode == "shakeout" and sig["exit_soft"][k] and not sig["exit_chop"][k]
                                and tf_absorbed[k] and defers < ov.max_defer):
                            defers += 1
                        else:
                            pending = ("chandelier" if spec.name == "regime" and sig["exit_soft"][k]
                                       else "regime_chop" if spec.name == "regime"
                                       else "below_ema", 1.0)
            j += 1
        if remaining > 1e-9:
            j = nf - 1 if exit_j is None else exit_j
            legs.append((remaining, fc[j], False, "eod_mark"))
            exit_j = j
        net = sum(f * costs.net_return(entry, px, entry_maker=entry_maker, exit_maker=mk)
                  for f, px, mk, _r in legs)
        wpx = sum(f * px for f, px, _m, _r in legs)
        ex_tf = tf_pos.get(int((ft[exit_j] // tfs) * tfs), n - 1)
        trades.append(FTrade(entry_i=e, exit_i=ex_tf, entry_time=int(t_tf[e]), exit_time=int(ft[exit_j]),
                             entry_px=float(entry), exit_px=float(wpx), stop_frac=float(stop_frac),
                             gross=float(wpx / entry - 1), net=float(net),
                             reason="+".join(r for _f, _p, _m, r in legs),
                             legs=[(round(f, 4), float(px), bool(mk), r) for f, px, mk, r in legs]))
        i = ex_tf + 1
    return trades


def FINE_SECONDS(fb: pd.DataFrame) -> int:  # noqa: N802
    t = fb["time"].to_numpy(np.int64)
    return int(np.median(np.diff(t))) if len(t) > 1 else 900

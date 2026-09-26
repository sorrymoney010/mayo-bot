"""Fee-aware bar-replay + walk-forward engine (offline, no network, no orders).

Used by ``scripts/backtest_walkforward.py``. Everything here is pure-pandas so
it is unit-testable and can never touch an exchange.

Execution model (deliberately conservative):
* Signals are computed on a CLOSED bar and filled at the NEXT bar's open.
* Every fill pays ``fee_bps`` (taker by default) plus ``slippage_bps`` against us.
* Stops / take-profits are checked intrabar from the entry bar onward. If a bar
  touches both, the STOP is assumed to fill first. A bar that gaps through a
  level fills at the open (worse than the level).
* One position per symbol, long-only spot (matches the paper engine).
"""
from __future__ import annotations

import itertools
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable

import numpy as np
import pandas as pd

from .indicators import compute_adx


# ── indicators ────────────────────────────────────────────────

def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def adx_hysteresis(adx: pd.Series, enter: float, exit_: float) -> pd.Series:
    """Vectorised version of ``indicators.adx_trend_allowed`` (per bar state)."""
    out = np.zeros(len(adx), dtype=bool)
    allow = False
    for i, v in enumerate(adx.to_numpy()):
        if math.isfinite(v):
            if v > enter:
                allow = True
            elif v < exit_:
                allow = False
        out[i] = allow
    return pd.Series(out, index=adx.index)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["ema20"] = _ema(d["close"], 20)
    d["ema50"] = _ema(d["close"], 50)
    d["ema200"] = _ema(d["close"], 200)
    d["rsi"] = _rsi(d["close"], 14)
    d["atr"] = _atr(d, 14)
    d["atr_pct"] = d["atr"] / d["close"]
    d["adx"] = compute_adx(d["high"], d["low"], d["close"], 14)
    # ATR% percentile vs the trailing 200 bars (volatility-expansion gate).
    d["atr_rank"] = d["atr_pct"].rolling(200, min_periods=50).rank(pct=True)
    return d


def regime_signals(d: pd.DataFrame, p: dict) -> dict[str, np.ndarray]:
    """Per-bar entry/exit arrays for the regime-switch trend sleeve.

    Shared by the backtester and the live ``RegimeTrendStrategy`` so both
    evaluate the identical rule set on closed bars.
    """
    adx_on = adx_hysteresis(d["adx"], p.get("adx_enter", 25.0), p.get("adx_exit", 20.0)).to_numpy()
    lb = int(p.get("lookback", 20))
    close = d["close"].to_numpy(float)
    prior_high = d["high"].shift(1).rolling(lb).max().to_numpy(float)
    hh22 = d["high"].rolling(22, min_periods=1).max().to_numpy(float)
    atr = d["atr"].to_numpy(float)
    rank = d["atr_rank"].to_numpy(float)
    min_rank = float(p.get("min_atr_rank", 0.0))
    vol_ok = np.ones(len(d), dtype=bool) if min_rank <= 0 else (np.nan_to_num(rank, nan=-1) >= min_rank)
    entry = (
        adx_on & vol_ok
        & (close > d["ema200"].to_numpy(float))
        & (d["ema20"].to_numpy(float) > d["ema50"].to_numpy(float))
        & (close > np.nan_to_num(prior_high, nan=np.inf))
        & np.isfinite(atr)
    )
    chandelier = hh22 - float(p.get("atr_mult", 3.0)) * atr
    return {"adx_on": adx_on, "entry": entry, "vol_ok": vol_ok, "prior_high": prior_high,
            "chandelier": chandelier, "chandelier_exit": close < chandelier}


def classify_adx_regime(bars: pd.DataFrame, enter: float = 25.0, exit_: float = 20.0) -> str:
    """'trend' | 'chop' | 'volatile_chop' for the last closed bar (learner key)."""
    if bars is None or len(bars) < 60:
        return "unknown"
    d = add_indicators(bars.sort_index())
    on = adx_hysteresis(d["adx"], enter, exit_).to_numpy()
    return regime_label(bool(on[-1]), float(d["atr_rank"].iloc[-1]))


# ── cost model ────────────────────────────────────────────────

@dataclass(frozen=True)
class Costs:
    fee_bps: float = 40.0        # per side, taker (Kraken tier-1 taker 0.40%)
    slippage_bps: float = 5.0    # per side, adverse, taker fills only
    maker_bps: float = 25.0      # per side, resting limit fills (Kraken tier-1 maker 0.25%)

    def net_return(self, entry_px: float, exit_px: float, *,
                   entry_maker: bool = False, exit_maker: bool = False) -> float:
        slip = self.slippage_bps / 1e4
        buy = entry_px * (1 if entry_maker else 1 + slip)
        sell = exit_px * (1 if exit_maker else 1 - slip)
        fin = (self.maker_bps if entry_maker else self.fee_bps) / 1e4
        fout = (self.maker_bps if exit_maker else self.fee_bps) / 1e4
        return (sell * (1 - fout)) / (buy * (1 + fin)) - 1.0


@dataclass
class Trade:
    entry_i: int
    exit_i: int
    entry_time: int
    exit_time: int
    entry_px: float
    exit_px: float
    stop_frac: float      # initial stop distance as fraction of entry (for sizing)
    gross: float          # raw price return
    net: float            # after fees + slippage
    reason: str
    regime: str = "unknown"


# ── strategies (signal generators) ────────────────────────────
# Each returns a dict of numpy arrays aligned with df:
#   entry[i]  – True if bar i CLOSE generates a BUY (fill at i+1 open)
# and a per-strategy exit spec consumed by ``simulate``.

@dataclass(frozen=True)
class Spec:
    name: str
    params: dict

    def label(self) -> str:
        ps = ",".join(f"{k}={v}" for k, v in sorted(self.params.items()))
        return f"{self.name}({ps})"


def regime_label(row_adx_on: bool, atr_rank: float) -> str:
    if row_adx_on:
        return "trend"
    if math.isfinite(atr_rank) and atr_rank >= 0.7:
        return "volatile_chop"
    return "chop"


def simulate(d: pd.DataFrame, spec: Spec, costs: Costs) -> list[Trade]:
    """Replay one strategy over an indicator-enriched frame."""
    p = spec.params
    n = len(d)
    o, h, l, c = (d[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    v = d["volume"].to_numpy(float)
    t = d["time"].to_numpy()
    atr = d["atr"].to_numpy(float)
    adx_on = adx_hysteresis(d["adx"], p.get("adx_enter", 25.0), p.get("adx_exit", 20.0)).to_numpy()
    atr_rank = d["atr_rank"].to_numpy(float)
    ema20, ema50, ema200 = (d[k].to_numpy(float) for k in ("ema20", "ema50", "ema200"))
    rsi = d["rsi"].to_numpy(float)

    lb = int(p.get("lookback", 20))
    prior_high = d["high"].shift(1).rolling(lb).max().to_numpy(float)
    vol_avg = d["volume"].shift(1).rolling(lb).mean().to_numpy(float)
    rsig = regime_signals(d, p) if spec.name == "regime" else None

    warm = 210
    trades: list[Trade] = []
    i = warm
    while i < n - 1:
        # ---- entry condition on bar i close ----
        if spec.name == "breakout":
            sig = c[i] > prior_high[i] and v[i] > vol_avg[i]
        elif spec.name == "momentum":
            sig = (
                adx_on[i]
                and p.get("rsi_min", 35) <= rsi[i] <= p.get("rsi_max", 75)
                and c[i] > ema200[i]
            )
        elif spec.name == "regime":
            sig = bool(rsig["entry"][i])
        elif spec.name == "meanrev":
            # Mirrors strategy.MeanReversionStrategy: RSI washed out AND below slow EMA.
            sig = rsi[i] <= p.get("rsi_os", 38.0) and c[i] < ema50[i]
        else:
            raise ValueError(spec.name)
        if not sig or not math.isfinite(atr[i]):
            i += 1
            continue

        regime = regime_label(bool(adx_on[i]), atr_rank[i])
        e = i + 1
        entry_maker = p.get("entry", "market") == "limit"
        if entry_maker:
            # Resting limit buy just under the signal close, good for ONE bar.
            limit = c[i] * (1 - p.get("limit_offset", 0.001))
            if l[e] > limit:
                i += 1          # not filled → no trade (adverse selection is real)
                continue
            entry = min(o[e], limit)
        else:
            entry = o[e]
        if spec.name in ("regime", "meanrev"):
            stop_frac = p.get("stop", 0.03)
            stop = entry * (1 - stop_frac)
            tp = entry * (1 + p.get("tp", 0.25))
        elif spec.name == "momentum":
            stop_frac = p.get("stop", 0.02)
            stop = entry * (1 - stop_frac)
            tp = entry * (1 + p.get("tp", 0.04))
        else:
            stop_frac = p.get("stop", 0.02)
            stop = entry * (1 - stop_frac)
            tp = entry * (1 + p.get("tp", 0.04))

        exit_px = None
        reason = ""
        j = e
        while j < n:
            # intrabar protective levels (stop first — conservative)
            if o[j] <= stop and j > e:
                exit_px, reason = o[j], "stop_gap"
            elif l[j] <= stop:
                exit_px, reason = stop, "stop"
            elif o[j] >= tp and j > e:
                exit_px, reason = o[j], "tp_gap"
            elif h[j] >= tp:
                exit_px, reason = tp, "tp"
            if exit_px is not None:
                break
            # close-based signal exits → fill next open
            sig_exit = False
            if spec.name == "momentum" and c[j] < ema50[j]:
                sig_exit, reason = True, "below_ema50"
            elif spec.name == "meanrev" and (
                rsi[j] >= p.get("rsi_exit", 55.0) or c[j] >= ema50[j]
            ):
                sig_exit, reason = True, "reverted"
            elif spec.name == "regime":
                # Stateless chandelier (same rule the live RegimeTrendStrategy
                # uses): close below 22-bar highest high - k*ATR → exit.
                if rsig["chandelier_exit"][j]:
                    sig_exit, reason = True, "chandelier"
                elif p.get("exit_on_chop", True) and not adx_on[j]:
                    sig_exit, reason = True, "regime_chop"
            if sig_exit and j + 1 < n:
                j += 1
                exit_px = o[j]
                break
            j += 1
        if exit_px is None:  # still open at data end → mark at last close
            j = n - 1
            exit_px, reason = c[j], "eod_mark"
        trades.append(Trade(
            entry_i=e, exit_i=j, entry_time=int(t[e]), exit_time=int(t[j]),
            entry_px=float(entry), exit_px=float(exit_px), stop_frac=float(stop_frac),
            gross=float(exit_px / entry - 1),
            net=float(costs.net_return(entry, exit_px, entry_maker=entry_maker,
                                       exit_maker=reason == "tp")),
            reason=reason, regime=regime,
        ))
        i = j + 1
    return trades


# ── metrics ───────────────────────────────────────────────────

@dataclass
class Metrics:
    trades: int = 0
    wins: int = 0
    win_rate: float = 0.0
    avg_net_bps: float = 0.0
    avg_gross_bps: float = 0.0
    expectancy_usd: float = 0.0
    total_return_pct: float = 0.0
    max_dd_pct: float = 0.0
    profit_factor: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def metrics(trades: Iterable[Trade], *, equity: float = 500.0, risk: float = 0.01,
            max_pos_frac: float = 0.40) -> Metrics:
    tr = sorted(trades, key=lambda x: x.entry_time)
    m = Metrics(trades=len(tr))
    if not tr:
        return m
    eq = equity
    peak = equity
    max_dd = 0.0
    pnls = []
    for x in tr:
        notional = min(eq * risk / max(x.stop_frac, 1e-6), eq * max_pos_frac)
        pnl = notional * x.net
        pnls.append(pnl)
        eq += pnl
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak)
    m.wins = sum(1 for x in tr if x.net > 0)
    m.win_rate = m.wins / len(tr)
    m.avg_net_bps = float(np.mean([x.net for x in tr]) * 1e4)
    m.avg_gross_bps = float(np.mean([x.gross for x in tr]) * 1e4)
    m.expectancy_usd = float(np.mean(pnls))
    m.total_return_pct = (eq / equity - 1) * 100
    m.max_dd_pct = max_dd * 100
    gains = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p < 0)
    m.profit_factor = gains / losses if losses > 0 else (math.inf if gains > 0 else 0.0)
    return m


# ── walk-forward ──────────────────────────────────────────────

def default_grid() -> dict[str, list[Spec]]:
    """Family -> candidate specs. The FIRST spec of each family is its default
    (what the paper bot would run). ``*_mk`` families use resting limit (maker)
    entries that only fill if the next bar trades through the limit."""
    g: dict[str, list[Spec]] = {"breakout": [], "momentum": [], "regime": []}
    for lb, (st, tp) in itertools.product((20, 55), ((0.02, 0.04), (0.03, 0.06), (0.015, 0.045))):
        g["breakout"].append(Spec("breakout", {"lookback": lb, "stop": st, "tp": tp}))
    for enter, (st, tp) in itertools.product((25.0, 30.0), ((0.02, 0.04), (0.03, 0.06))):
        g["momentum"].append(Spec("momentum", {"adx_enter": enter, "adx_exit": 20.0,
                                               "stop": st, "tp": tp}))
    for lb, am, rank, st in itertools.product((20, 55), (3.0, 2.0), (0.0, 0.5), (0.03, 0.05)):
        g["regime"].append(Spec("regime", {"lookback": lb, "atr_mult": am,
                                           "min_atr_rank": rank, "stop": st, "tp": 0.25,
                                           "adx_enter": 25.0, "adx_exit": 20.0}))
    g["meanrev"] = [Spec("meanrev", {"rsi_os": os_, "rsi_exit": 55.0, "stop": st, "tp": 0.25})
                    for os_, st in itertools.product((38.0, 30.0), (0.03, 0.05))]
    for fam in ("breakout", "regime", "meanrev"):
        g[f"{fam}_mk"] = [Spec(sp.name, {**sp.params, "entry": "limit", "limit_offset": 0.001})
                          for sp in g[fam]]
    return g


@dataclass
class WalkForwardResult:
    family: str
    is_trades: list[Trade] = field(default_factory=list)
    oos_trades: list[Trade] = field(default_factory=list)
    chosen: list[str] = field(default_factory=list)


def walk_forward(d: pd.DataFrame, specs: list[Spec], costs: Costs, *, folds: int = 4,
                 warm: int = 210, min_is_trades: int = 3,
                 sim: Callable[[pd.DataFrame, Spec, Costs], list[Trade]] = simulate,
                 ) -> WalkForwardResult:
    """Rolling walk-forward: pick the best spec on segment k-1 (IS) by net
    expectancy, then score it on segment k (OOS). Trades are attributed to a
    segment by ENTRY bar; each spec is simulated once over the full series so
    positions are not artificially cut at fold boundaries."""
    res = WalkForwardResult(family=specs[0].name)
    n = len(d)
    usable = n - warm
    if usable < (folds + 1) * 20:
        return res
    edges = [warm + (usable * k) // (folds + 1) for k in range(folds + 2)]
    all_trades = {s.label(): (s, sim(d, s, costs)) for s in specs}

    def seg(trs: list[Trade], lo: int, hi: int) -> list[Trade]:
        return [x for x in trs if lo <= x.entry_i < hi]

    for k in range(1, folds + 1):
        is_lo, is_hi = edges[k - 1], edges[k]
        oos_lo, oos_hi = edges[k], edges[k + 1]
        best_label, best_score = None, -math.inf
        for label, (_s, trs) in all_trades.items():
            is_tr = seg(trs, is_lo, is_hi)
            if len(is_tr) < min_is_trades:
                continue
            score = float(np.mean([x.net for x in is_tr]))
            if score > best_score:
                best_label, best_score = label, score
        if best_label is None:
            # Nothing traded enough in-sample → fall back to the first (default) spec.
            best_label = specs[0].label()
        _s, trs = all_trades[best_label]
        res.chosen.append(best_label)
        res.is_trades.extend(seg(trs, is_lo, is_hi))
        res.oos_trades.extend(seg(trs, oos_lo, oos_hi))
    return res

"""Kraken Futures perpetual strategy research (PAPER research only, offline).

Generic long/short perp simulator with:
* next-bar-open fills, taker fee + slippage per side (published base tier 5 bps taker),
* real hourly funding (positive rate: longs pay shorts), summed per bar held,
* isolated-margin liquidation on the MARK high/low (gap -> open), loss capped at
  the full margin (1/leverage of notional) when liquidated,
* walk-forward by TIME segments (repo convention: warm-up, 5 segments, pick the
  best spec on segment k-1 by mean net, score it on segment k; trades attributed
  by entry bar; every spec simulated once over the whole series).

Nothing here places orders, reads keys or touches the live bot.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .backtest_core import add_indicators
from .daily_filter import riskon_table
from .learner import mean_bounds

TAKER_BPS = 5.0
MAKER_BPS = 2.0
MAINT = 0.01


# ───────────────────────── data ─────────────────────────

def load_perp_hourly(data_dir, perp: str) -> pd.DataFrame:
    """Hourly frame: trade OHLCV + mark OHLC + index close + funding (NaN where absent)."""
    from pathlib import Path
    d = Path(data_dir)
    tr = pd.read_csv(d / f"futures_{perp}_60_trade.csv")
    mk = pd.read_csv(d / f"futures_{perp}_60_mark.csv")
    sp = pd.read_csv(d / f"futures_{perp}_60_spot.csv")
    df = tr.merge(mk[["time", "open", "high", "low", "close"]].rename(
        columns={"open": "m_open", "high": "m_high", "low": "m_low", "close": "m_close"}), on="time", how="left")
    df = df.merge(sp[["time", "open", "close"]].rename(columns={"open": "s_open", "close": "s_close"}),
                  on="time", how="left")
    df = df.sort_values("time").drop_duplicates("time").reset_index(drop=True)
    # Regular hourly grid; forward-fill price gaps (no trades in an hour -> flat bar).
    grid = pd.DataFrame({"time": np.arange(int(df.time.iloc[0]), int(df.time.iloc[-1]) + 1, 3600)})
    df = grid.merge(df, on="time", how="left")
    df["close"] = df["close"].ffill()
    for k in ("open", "high", "low"):
        df[k] = df[k].fillna(df["close"])
    df["volume"] = df["volume"].fillna(0.0)
    for k in ("m_open", "m_high", "m_low", "m_close"):
        df[k] = df[k].fillna(df[{"m_open": "open", "m_high": "high", "m_low": "low", "m_close": "close"}[k]])
    df["s_close"] = df["s_close"].ffill()
    df["s_open"] = df["s_open"].fillna(df["s_close"])
    fr = pd.read_csv(d / f"futures_funding_{perp}.csv")
    # A rate stamped T is realised for the hour [T-1h, T): charge it to the bar opening at T-1h.
    fr = fr.assign(time=fr["time"] - 3600)[["time", "rate"]]
    df = df.merge(fr, on="time", how="left")
    return df


def resample(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    if minutes == 60:
        return df.copy()
    k = minutes * 60
    g = df.assign(bucket=(df["time"] // k) * k).groupby("bucket", sort=True)
    out = pd.DataFrame({
        "time": g["time"].first().index.astype(np.int64),
        "open": g["open"].first().to_numpy(), "high": g["high"].max().to_numpy(),
        "low": g["low"].min().to_numpy(), "close": g["close"].last().to_numpy(),
        "volume": g["volume"].sum().to_numpy(),
        "m_open": g["m_open"].first().to_numpy(), "m_high": g["m_high"].max().to_numpy(),
        "m_low": g["m_low"].min().to_numpy(), "m_close": g["m_close"].last().to_numpy(),
        "s_open": g["s_open"].first().to_numpy(), "s_close": g["s_close"].last().to_numpy(),
        # Sum of hourly rates inside the bar (NaN if none known).
        "rate": g["rate"].sum(min_count=1).to_numpy(),
        "rate_n": g["rate"].count().to_numpy(),
    })
    return out.reset_index(drop=True)


def daily_from_hourly(df: pd.DataFrame) -> pd.DataFrame:
    g = df.assign(day=(df["time"] // 86400) * 86400).groupby("day", sort=True)
    return pd.DataFrame({"time": g["time"].first().index.astype(np.int64), "close": g["close"].last().to_numpy()})


def attach_daily(d: pd.DataFrame, daily: pd.DataFrame, tf_minutes: int, *, now: float) -> pd.DataFrame:
    """d1_riskon (repo rule) and d1_bear_strict (close<SMA50 and SMA50 falling vs 5d ago),
    using the latest CLOSED day at or before each bar's close (no look-ahead)."""
    tab = riskon_table(daily, now=now)
    d = d.copy()
    bar_close = d["time"].to_numpy(float) + tf_minutes * 60
    ct = tab["close_time"].to_numpy(float)
    idx = np.searchsorted(ct, bar_close, side="right") - 1
    ok = idx >= 0
    on = np.full(len(d), np.nan)
    bear = np.full(len(d), np.nan)
    day_close = np.full(len(d), np.nan)
    riskon = tab["riskon"].to_numpy(float)
    strict = np.where(np.isnan(riskon), np.nan,
                      ((tab["close"] < tab["sma50"]) & (tab["sma50"] < tab["sma50_prev"])).astype(float))
    on[ok] = riskon[idx[ok]]
    bear[ok] = strict[idx[ok]]
    day_close[ok] = tab["close_time"].to_numpy(float)[idx[ok]]
    d["d1_riskon"] = on
    d["d1_bear_strict"] = bear
    d["d1_close_time"] = day_close
    return d


def prepare(df_hourly: pd.DataFrame, tf: int, *, now: float) -> pd.DataFrame:
    d = resample(df_hourly, tf)
    d = add_indicators(d)
    d = attach_daily(d, daily_from_hourly(df_hourly), tf, now=now)
    return d


# ───────────────────────── simulator ─────────────────────────

@dataclass
class PTrade:
    symbol: str
    side: int            # +1 long, -1 short
    entry_i: int
    exit_i: int
    entry_time: int
    exit_time: int
    entry_px: float
    exit_px: float
    gross: float         # price return per notional, signed for side
    fees: float          # fees + slippage per notional (positive = cost)
    funding: float       # funding per notional (positive = received)
    net: float           # per notional, after everything (>= -1/lev on liquidation)
    reason: str
    leverage: float
    path: list = field(default_factory=list, repr=False)  # (time, mtm_net) per bar for equity curves


@dataclass
class Costs:
    fee_bps: float = TAKER_BPS
    slip_bps: float = 2.0

    @property
    def side_cost(self) -> float:
        return (self.fee_bps + self.slip_bps) / 1e4


def liq_price(entry: float, side: int, lev: float, mm: float = MAINT) -> float:
    if lev <= 1.0 + 1e-9 and side > 0:
        return 0.0  # a 1x isolated long cannot be liquidated above zero (margin = notional)
    mm = min(mm, 1.0 / lev - 1e-9)
    return entry * (1.0 + 1.0 / lev - mm) if side < 0 else entry * (1.0 - 1.0 / lev + mm)


def simulate(d: pd.DataFrame, sig: dict, *, symbol: str, lev: float, costs: Costs,
             warm: int, max_hold: int = 0, record_path: bool = True) -> list[PTrade]:
    """``sig`` holds bool arrays evaluated at bar CLOSE: long_entry, short_entry and
    optionally long_exit, short_exit. Fill at the next bar's open. An opposite entry
    while in a position closes it (and the next bar can re-enter the other way)."""
    n = len(d)
    o, c = d["open"].to_numpy(float), d["close"].to_numpy(float)
    mo, mh, ml = d["m_open"].to_numpy(float), d["m_high"].to_numpy(float), d["m_low"].to_numpy(float)
    t = d["time"].to_numpy(np.int64)
    rate = np.nan_to_num(d["rate"].to_numpy(float), nan=0.0)
    le, se = sig["long_entry"], sig["short_entry"]
    lx = sig.get("long_exit", np.zeros(n, bool))
    sx = sig.get("short_exit", np.zeros(n, bool))
    sc = costs.side_cost
    trades: list[PTrade] = []
    i = warm
    while i < n - 1:
        side = 1 if le[i] else (-1 if se[i] else 0)
        if side == 0:
            i += 1
            continue
        e = i + 1
        entry = o[e]
        liq = liq_price(entry, side, lev)
        fund = 0.0
        exit_px, reason, j = None, "", e
        path = []
        while j < n:
            # Liquidation on mark (gap at open first, then wick).
            if side > 0 and liq > 0:
                if j > e and mo[j] <= liq:
                    exit_px, reason = mo[j], "liquidation_gap"
                elif ml[j] <= liq:
                    exit_px, reason = liq, "liquidation"
            elif side < 0:
                if j > e and mo[j] >= liq:
                    exit_px, reason = mo[j], "liquidation_gap"
                elif mh[j] >= liq:
                    exit_px, reason = liq, "liquidation"
            if exit_px is not None:
                break
            fund += -side * rate[j]
            if record_path:
                path.append((int(t[j]) , side * (c[j] / entry - 1) - sc + fund))
            stop = (side > 0 and (lx[j] or se[j])) or (side < 0 and (sx[j] or le[j]))
            if max_hold and (j - e + 1) >= max_hold:
                stop, why = True, "max_hold"
            else:
                why = "signal_exit"
            if stop and j + 1 < n:
                j += 1
                exit_px, reason = o[j], why
                break
            j += 1
        if exit_px is None:
            j = n - 1
            exit_px, reason = c[j], "eod_mark"
        gross = side * (exit_px / entry - 1)
        fees = 2 * sc
        net = gross - fees + fund
        if reason.startswith("liquidation"):
            net = -1.0 / lev  # whole isolated margin gone (liquidation fee absorbed)
        trades.append(PTrade(symbol, side, e, j, int(t[e]), int(t[j]), float(entry), float(exit_px),
                             float(gross), float(fees), float(fund), float(net), reason, lev,
                             path if record_path else []))
        # Exited at open of bar j: allow re-entry on bar j's close.
        i = j
    return trades


# ───────────────────────── walk-forward ─────────────────────────

def time_edges(t0: int, t1: int, folds: int = 4) -> list[int]:
    return [int(t0 + (t1 - t0) * k / (folds + 1)) for k in range(folds + 2)]


def walk_forward(spec_trades: dict[str, list[PTrade]], edges: list[int], default: str,
                 min_is_trades: int = 3) -> tuple[list[PTrade], list[str]]:
    """spec_trades: label -> trades pooled across symbols (full series)."""
    oos, chosen = [], []
    folds = len(edges) - 2
    for k in range(1, folds + 1):
        is_lo, is_hi, oo_lo, oo_hi = edges[k - 1], edges[k], edges[k], edges[k + 1]
        best, score = None, -math.inf
        for lab, trs in spec_trades.items():
            seg = [x.net for x in trs if is_lo <= x.entry_time < is_hi]
            if len(seg) < min_is_trades:
                continue
            m = float(np.mean(seg))
            if m > score:
                best, score = lab, m
        best = best or default
        chosen.append(best)
        oos.extend(x for x in spec_trades[best] if oo_lo <= x.entry_time < oo_hi)
    return oos, chosen


# ───────────────────────── metrics ─────────────────────────

def equity_curve(trades: list[PTrade], symbols: list[str], t0: int, t1: int,
                 lev: float, capital_mult: float = 1.0) -> tuple[float, float]:
    """Equal capital per symbol sleeve; each trade uses notional = lev x sleeve equity
    (capital_mult scales capital needs, e.g. 2.0 for a fully funded two-leg carry).
    Hourly mark-to-market. Returns (total return %, max drawdown %)."""
    hours = np.arange(t0 - t0 % 3600, t1 + 3600, 3600, dtype=np.int64)
    curve = np.zeros(len(hours))
    for s in symbols:
        eq = np.full(len(hours), np.nan)
        e_now = 1.0 / len(symbols)
        last_idx = 0
        for tr in sorted((x for x in trades if x.symbol == s), key=lambda x: x.entry_time):
            k0 = np.searchsorted(hours, tr.entry_time)
            eq[last_idx:k0] = np.where(np.isnan(eq[last_idx:k0]), e_now, eq[last_idx:k0])
            base = e_now
            for (tt, mtm) in tr.path:
                k = np.searchsorted(hours, tt)
                if k < len(hours):
                    eq[k] = base * (1 + lev * mtm / capital_mult)
            e_now = base * (1 + lev * tr.net / capital_mult)
            k1 = min(np.searchsorted(hours, tr.exit_time), len(hours) - 1)
            eq[k1] = e_now
            last_idx = k1 + 1
        eq[last_idx:] = np.where(np.isnan(eq[last_idx:]), e_now, eq[last_idx:])
        eq = pd.Series(eq).ffill().fillna(1.0 / len(symbols)).to_numpy()
        curve += eq
    peak = np.maximum.accumulate(curve)
    dd = float(np.max((peak - curve) / peak)) if len(curve) else 0.0
    return float((curve[-1] - 1.0) * 100) if len(curve) else 0.0, dd * 100


def summarize(trades: list[PTrade], *, lev: float, symbols: list[str], t0: int, t1: int,
              capital_mult: float = 1.0) -> dict:
    bps = [x.net * 1e4 for x in trades]
    n = len(bps)
    out = {"trades": n}
    if n == 0:
        out.update(passes=False, failed=["min_trades"])
        return out
    mean, lo, _ = mean_bounds(bps, 0.90)
    srt = sorted(bps)
    ex2 = float(np.mean(srt[:-2])) if n > 2 else None
    ret, dd = equity_curve(trades, symbols, t0, t1, lev, capital_mult)
    checks = {
        "min_trades": n >= 30,
        "mean_positive": mean > 0,
        "lower_bound_positive": lo is not None and lo > 0,
        "ex_best_2_positive": ex2 is not None and ex2 > 0,
    }
    out.update({
        "mean_net_bps": round(mean, 1), "lcb90_bps": None if lo is None else round(lo, 1),
        "median_net_bps": round(float(np.median(bps)), 1),
        "mean_ex_best2_bps": None if ex2 is None else round(ex2, 1),
        "win_rate": round(sum(1 for b in bps if b > 0) / n, 3),
        "longs": sum(1 for x in trades if x.side > 0), "shorts": sum(1 for x in trades if x.side < 0),
        "mean_funding_bps": round(float(np.mean([x.funding for x in trades])) * 1e4, 1),
        "mean_fees_bps": round(float(np.mean([x.fees for x in trades])) * 1e4, 1),
        "liquidations": sum(1 for x in trades if x.reason.startswith("liquidation")),
        "oos_return_pct": round(ret, 2), "max_dd_pct": round(dd, 2),
        "checks": checks, "passes": all(checks.values()),
        "failed": [k for k, v in checks.items() if not v],
    })
    return out


def buy_hold(frames: dict[str, pd.DataFrame], t0: int, t1: int, spot_side_cost: float) -> dict:
    """Equal-weight spot buy-and-hold on the index price, one round trip of spot costs."""
    rets = []
    curves = []
    for d in frames.values():
        m = (d["time"] >= t0) & (d["time"] < t1)
        px = d.loc[m, "s_close"].to_numpy(float)
        if len(px) < 2:
            continue
        r = px / px[0] * (1 - spot_side_cost)
        curves.append(r / len(frames))
        rets.append((px[-1] / px[0]) * (1 - spot_side_cost) ** 2 - 1)
    L = min(len(c) for c in curves)
    curve = np.sum([c[:L] for c in curves], axis=0)
    peak = np.maximum.accumulate(curve)
    return {"per_symbol_return_pct": {s: round(r * 100, 2) for s, r in zip(frames, rets, strict=True)},
            "equal_weight_return_pct": round(float(np.mean(rets)) * 100, 2),
            "equal_weight_max_dd_pct": round(float(np.max((peak - curve) / peak)) * 100, 2)}


def funding_proxy_fill(h: pd.DataFrame) -> pd.DataFrame:
    """Fill hours WITHOUT published funding with a proxy: OLS of the real hourly
    relative rate on the hourly mark premium ((mark - index) / index) fitted on the
    overlap, clipped to the contract cap (0.5%/h). Real rates are kept where known.
    The fit (slope, intercept, correlation, n) is stored in ``attrs['proxy_fit']``."""
    h = h.copy()
    prem = (h["m_close"] - h["s_close"]) / h["s_close"]
    m = h["rate"].notna() & prem.notna()
    b, a = np.polyfit(prem[m], h.loc[m, "rate"], 1)
    corr = float(np.corrcoef(prem[m], h.loc[m, "rate"])[0, 1])
    proxy = (a + b * prem).clip(-0.005, 0.005)
    h["rate_is_proxy"] = h["rate"].isna()
    h["rate"] = h["rate"].fillna(proxy)
    h.attrs["proxy_fit"] = {"slope": float(b), "intercept": float(a), "corr_on_overlap": round(corr, 3),
                            "overlap_hours": int(m.sum()),
                            "proxy_mean_hourly_pre_window": float(proxy[h["rate_is_proxy"]].mean())}
    return h

#!/usr/bin/env python3
"""Helpers for the daily trend-following quick check. Research only."""
from __future__ import annotations
import json, math, zipfile
from pathlib import Path
import numpy as np
import pandas as pd

VISION = Path("/workspace/edge2-data/daily_trend/vision")
KRAKEN_DIR = Path("/workspace/edge2-data/daily_trend")
END = pd.Timestamp("2026-11-14")
DEV_END = pd.Timestamp("2023-12-31")
TEST_START = pd.Timestamp("2024-01-01")
YTD_START = pd.Timestamp("2026-01-01")
LOOKBACKS = [5, 10, 20, 30, 60, 90, 150, 250, 360]
VOL_WIN, VOL_TARGET, ANN, REBAL_THRESH = 90, 0.25, 365, 0.20
COSTS = {"10bps": 0.0010, "80bps": 0.0080, "160bps": 0.0160}
START_EQUITY = 100.0
N_PERM, PERM_SEED = 1000, 20261115
ORDERMIN = {"BTC": 0.00006, "ETH": 0.002, "SOL": 0.05}
COSTMIN = {"BTC": 0.50, "ETH": 0.50, "SOL": 0.50}
ASSETS = ["BTC", "ETH", "SOL"]
SYM = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}

def load_vision(symbol: str) -> pd.DataFrame:
    rows = []
    for zpath in sorted(VISION.glob(f"{symbol}-1d-*.zip")):
        with zipfile.ZipFile(zpath) as z:
            for name in z.namelist():
                if not name.endswith(".csv"):
                    continue
                for line in z.read(name).decode().splitlines():
                    if not line.strip():
                        continue
                    p = line.split(",")
                    rows.append((int(p[0]), float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5])))
    df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c", "v"])
    # Vision switched from millisecond to microsecond open-times around 2025.
    ts = df["ts"].astype("int64")
    unit = np.where(ts >= 10**15, "us", np.where(ts >= 10**12, "ms", "s"))
    # parse in groups by unit
    dates = pd.Series(pd.NaT, index=df.index)
    for u in ("us", "ms", "s"):
        m = unit == u
        if m.any():
            dates.loc[m] = pd.to_datetime(ts[m], unit=u, utc=True).dt.tz_convert(None).dt.normalize()
    df["date"] = dates
    df = df.drop_duplicates("date").sort_values("date").set_index("date")
    return df.loc[df.index <= END, ["o", "h", "l", "c", "v"]]

def load_kraken(asset: str) -> pd.DataFrame:
    d = json.loads((KRAKEN_DIR / f"kraken_{asset}USD_1440.json").read_text())
    df = pd.DataFrame(d["rows"], columns=["time", "o", "h", "l", "c", "vwap", "v", "count"])
    df = df.astype({c: float for c in ["o", "h", "l", "c", "vwap", "v"]})
    df["date"] = pd.to_datetime(df["time"], unit="s", utc=True).dt.tz_convert(None).dt.normalize()
    df = df.drop_duplicates("date").sort_values("date").set_index("date")
    return df.loc[df.index <= END, ["o", "h", "l", "c", "v"]]

def zarattini_pos(close: np.ndarray, n: int) -> np.ndarray:
    tlen = len(close)
    pos = np.zeros(tlen, dtype=np.int8)
    if tlen < n:
        return pos
    s = pd.Series(close)
    up = s.rolling(n, min_periods=n).max().to_numpy()
    dn = s.rolling(n, min_periods=n).min().to_numpy()
    mid = 0.5 * (up + dn)
    stop = np.nan
    in_pos = False
    for t in range(tlen):
        if not in_pos:
            if np.isfinite(up[t]) and close[t] >= up[t] - 1e-12:
                in_pos = True
                stop = mid[t]
                pos[t] = 1
        else:
            if close[t] <= stop:
                in_pos = False
                stop = np.nan
                pos[t] = 0
            else:
                pos[t] = 1
                if np.isfinite(mid[t]):
                    stop = max(stop, mid[t])
    return pos

def sma_pos(close: np.ndarray, n: int) -> np.ndarray:
    sma = pd.Series(close).rolling(n, min_periods=n).mean().to_numpy()
    pos = np.zeros(len(close), dtype=np.int8)
    valid = np.isfinite(sma)
    pos[valid] = (close[valid] > sma[valid]).astype(np.int8)
    return pos

def donchian_hl_pos(high, low, close, n_in, n_out) -> np.ndarray:
    tlen = len(close)
    pos = np.zeros(tlen, dtype=np.int8)
    prior_hi = pd.Series(high).shift(1).rolling(n_in, min_periods=n_in).max().to_numpy()
    prior_lo = pd.Series(low).shift(1).rolling(n_out, min_periods=n_out).min().to_numpy()
    in_pos = False
    for t in range(tlen):
        if not in_pos:
            if np.isfinite(prior_hi[t]) and close[t] >= prior_hi[t]:
                in_pos = True
                pos[t] = 1
        else:
            if np.isfinite(prior_lo[t]) and close[t] <= prior_lo[t]:
                in_pos = False
                pos[t] = 0
            else:
                pos[t] = 1
    return pos

def vol_ann(close, win=VOL_WIN) -> np.ndarray:
    ret = pd.Series(close).pct_change().to_numpy()
    return pd.Series(ret).rolling(win, min_periods=win).std().to_numpy() * math.sqrt(ANN)

def size_from_pos(pos, sigma, cap, use_vol) -> np.ndarray:
    tlen = len(pos)
    w = np.zeros(tlen, dtype=float)
    if not use_vol:
        return pos.astype(float)
    for t in range(tlen):
        if pos[t] == 0:
            continue
        s = sigma[t]
        if np.isfinite(s) and s > 1e-12:
            w[t] = min(VOL_TARGET / s, cap) * pos[t]
    return w

def apply_rebalance_threshold(target, pos_change, thresh) -> np.ndarray:
    tlen = len(target)
    applied = np.zeros(tlen, dtype=float)
    for t in range(tlen):
        if t == 0 or pos_change[t] or abs(target[t] - applied[t - 1]) > thresh:
            applied[t] = target[t]
        else:
            applied[t] = applied[t - 1]
    return applied

def combo_target(close, sigma, cap, use_vol, thresh):
    sleeves, poss = [], []
    for n in LOOKBACKS:
        p = zarattini_pos(close, n)
        poss.append(p)
        sleeves.append(size_from_pos(p, sigma, cap, use_vol))
    poss = np.stack(poss, axis=0)
    target = np.mean(np.stack(sleeves, axis=0), axis=0)
    pos_change = np.zeros(len(close), dtype=bool)
    pos_change[0] = True
    pos_change[1:] = np.any(poss[:, 1:] != poss[:, :-1], axis=0)
    applied = apply_rebalance_threshold(target, pos_change, thresh) if (use_vol and thresh is not None) else target
    return applied, (poss.sum(axis=0) > 0).astype(np.int8), poss


def max_drawdown(equity: np.ndarray) -> float:
    peak = np.maximum.accumulate(equity)
    dd = equity / np.where(peak > 0, peak, np.nan) - 1.0
    return float(np.nanmin(dd)) if len(dd) else float("nan")

def sharpe(returns: np.ndarray) -> float:
    r = returns[np.isfinite(returns)]
    if len(r) < 2:
        return float("nan")
    sd = r.std(ddof=1)
    return float("nan") if sd <= 0 else float(r.mean() / sd * math.sqrt(ANN))

def cagr(equity: np.ndarray, n_days: int) -> float:
    if n_days <= 0 or equity[0] <= 0 or equity[-1] <= 0:
        return float("nan")
    return float((equity[-1] / equity[0]) ** (ANN / n_days) - 1.0)

def trade_stats(pnls) -> dict:
    a = np.asarray(pnls, dtype=float)
    if len(a) == 0:
        return dict(n=0, win=float("nan"), pf=float("nan"), avg_win=float("nan"),
                    avg_loss=float("nan"), payoff=float("nan"), sum_pnl=0.0)
    wins, losses = a[a > 0], a[a < 0]
    gp = float(wins.sum()) if len(wins) else 0.0
    gl = float(-losses.sum()) if len(losses) else 0.0
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else float("nan"))
    avg_win = float(wins.mean()) if len(wins) else float("nan")
    avg_loss = float(losses.mean()) if len(losses) else float("nan")
    payoff = (avg_win / abs(avg_loss)) if (len(wins) and len(losses) and avg_loss != 0) else float("nan")
    return dict(n=int(len(a)), win=float((a > 0).mean()), pf=pf, avg_win=avg_win,
                avg_loss=avg_loss, payoff=payoff, sum_pnl=float(a.sum()))

class Book:
    """Daily book: weights decided at close t, filled at next open, earn open-to-open."""

    def __init__(self, idx, opens, closes, ready):
        self.idx, self.opens, self.closes, self.ready = idx, opens, closes, ready
        self.n = len(idx)
        self.asset_ret = {}
        for a in ASSETS:
            o = opens[a]
            r = np.full(self.n, np.nan)
            r[:-1] = o[1:] / o[:-1] - 1.0
            self.asset_ret[a] = r

    def run(self, decided, rt_cost, start_equity=START_EQUITY, start=None, end=None):
        n, idx = self.n, self.idx
        W = {a: np.zeros(n) for a in ASSETS}
        n_ready = np.zeros(n, dtype=int)
        for t in range(n):
            ready_t = [a for a in ASSETS if self.ready[a][t] and np.isfinite(decided[a][t])]
            n_ready[t] = len(ready_t)
            if not ready_t:
                continue
            share = 1.0 / len(ready_t)
            for a in ready_t:
                W[a][t] = share * decided[a][t]
        port_r = np.zeros(n)
        fee_r = np.zeros(n)
        gross_r = np.zeros(n)
        equity = np.full(n, np.nan)
        first = 0 if start is None else int(np.searchsorted(idx.values, np.datetime64(start)))
        last = n - 2
        if end is not None:
            last = min(last, int(np.searchsorted(idx.values, np.datetime64(end), side="right")) - 2)
        filled = {a: 0.0 for a in ASSETS}
        prev_w = {a: 0.0 for a in ASSETS}
        started, eq_start_i, eq = False, None, start_equity
        open_tr, closed, unfillable, fills = {}, [], [], []
        for t in range(0, n - 1):
            target_w = {a: 0.0 for a in ASSETS} if t == 0 else {a: W[a][t - 1] for a in ASSETS}
            in_window = (t >= first) and (t <= last)
            if in_window and not started:
                started, eq_start_i, eq = True, t, start_equity
                # a fresh book starts flat; do not inherit mid-position weights without opening a trade
                filled = {a: 0.0 for a in ASSETS}
                prev_w = {a: 0.0 for a in ASSETS}
            if started and in_window:
                day_fee = 0.0
                for a in ASSETS:
                    dw = target_w[a] - filled[a]
                    if abs(dw) < 1e-15:
                        continue
                    px = self.opens[a][t]
                    notional = abs(dw) * eq
                    min_notional = max(COSTMIN[a], ORDERMIN[a] * px) if np.isfinite(px) else COSTMIN[a]
                    fills.append(dict(date=str(idx[t].date()), asset=a, dw=float(dw),
                                      notional=float(notional), min_notional=float(min_notional), equity=float(eq)))
                    if 0 < notional < min_notional - 1e-12:
                        unfillable.append(dict(date=str(idx[t].date()), asset=a, notional=float(notional),
                                               min_notional=float(min_notional), dw=float(dw)))
                    day_fee += (rt_cost / 2.0) * abs(dw)
                    filled[a] = target_w[a]
                fee_r[t] = day_fee
                g, daily_pnl = 0.0, {}
                for a in ASSETS:
                    r = self.asset_ret[a][t]
                    if not np.isfinite(r):
                        r = 0.0
                    contrib = filled[a] * r
                    g += contrib
                    daily_pnl[a] = contrib * eq
                gross_r[t] = g
                net = g - day_fee
                port_r[t] = net
                dw_abs_sum = sum(abs(target_w[a] - prev_w[a]) for a in ASSETS)
                dollar = {}
                for a in ASSETS:
                    fee_attr = (day_fee * abs(target_w[a] - prev_w[a]) / dw_abs_sum * eq) if dw_abs_sum > 0 else 0.0
                    dollar[a] = daily_pnl[a] - fee_attr
                for a in ASSETS:
                    was, now = prev_w[a], filled[a]
                    if was == 0 and now > 0:
                        open_tr[a] = dict(asset=a, entry=str(idx[t].date()), entry_i=t,
                                          entry_px=float(self.opens[a][t]), pnl=dollar[a], entry_eq=float(eq))
                    elif was > 0 and now == 0 and a in open_tr:
                        open_tr[a]["pnl"] += dollar[a]
                        tr = open_tr.pop(a)
                        tr.update(exit=str(idx[t].date()), exit_i=t, exit_px=float(self.opens[a][t]),
                                  hold_days=int(t - tr["entry_i"]))
                        closed.append(tr)
                    elif now > 0 and a in open_tr:
                        open_tr[a]["pnl"] += dollar[a]
                prev_w = {a: filled[a] for a in ASSETS}
                eq = eq * (1.0 + net)
                equity[t] = eq
            else:
                pass
        if started:
            end_i = last
            for a, tr in list(open_tr.items()):
                tr["exit"] = str(idx[end_i + 1].date()) if end_i + 1 < n else str(idx[end_i].date())
                tr["exit_i"] = int(min(end_i + 1, n - 1))
                tr["exit_px"] = float(self.opens[a][tr["exit_i"]])
                tr["hold_days"] = int(tr["exit_i"] - tr["entry_i"])
                tr["open_at_end"] = True
                closed.append(tr)
        if eq_start_i is None:
            return dict(empty=True)
        return dict(empty=False, index=idx, port_r=port_r, gross_r=gross_r, fee_r=fee_r,
                    equity=equity, start_i=eq_start_i, last_i=last, final_equity=float(eq),
                    trades=closed, unfillable=unfillable, fills=fills, W=W, n_ready=n_ready)

def metrics_from_run(run, idx, start=None, end=None) -> dict:
    if run.get("empty"):
        return dict(empty=True)
    port_r, equity = run["port_r"], run["equity"]
    mask = np.isfinite(equity)
    if start is not None:
        mask &= np.asarray(idx >= start)
    if end is not None:
        mask &= np.asarray(idx <= end)
    days = np.where(mask)[0]
    if len(days) == 0:
        return dict(empty=True)
    r = port_r[mask]
    eq = np.cumprod(1.0 + r) * START_EQUITY
    def in_win(t):
        e = pd.Timestamp(t["entry"])
        if start is not None and e < start:
            return False
        if end is not None and e > end:
            return False
        return True
    ts = trade_stats([t["pnl"] for t in run["trades"] if in_win(t)])
    n_days = int(len(r))
    return dict(empty=False, n_days=n_days, start=str(idx[days[0]].date()), end=str(idx[days[-1]].date()),
                final_eq=float(eq[-1]), tot_ret=float(eq[-1] / START_EQUITY - 1.0),
                cagr=cagr(eq, n_days),
                vol=float(r.std(ddof=1) * math.sqrt(ANN)) if len(r) > 1 else float("nan"),
                sharpe=sharpe(r), maxdd=max_drawdown(eq), mean_day=float(r.mean()) if len(r) else float("nan"),
                **{f"tr_{k}": v for k, v in ts.items()})

def yearly_folds(run, idx, start=None, end=None):
    rows = []
    mask = np.isfinite(run["equity"])
    if start is not None:
        mask &= np.asarray(idx >= start)
    if end is not None:
        mask &= np.asarray(idx <= end)
    for y in sorted(set(idx[mask].year)):
        m = metrics_from_run(run, idx, start=pd.Timestamp(f"{y}-01-01"), end=pd.Timestamp(f"{y}-12-31"))
        if m.get("empty"):
            continue
        # skip pre-warmup (or otherwise inactive) years: no trades and a flat book
        if (m.get("tr_n") or 0) == 0 and abs(m.get("tot_ret") or 0) < 1e-15:
            continue
        rows.append(dict(year=y, tot_ret=m["tot_ret"], cagr=m["cagr"], sharpe=m["sharpe"],
                         maxdd=m["maxdd"], n=m["tr_n"], pf=m["tr_pf"], final_eq=m["final_eq"]))
    return rows

def buy_and_hold(book, rt_cost, start, end) -> dict:
    idx, n = book.idx, book.n
    first = int(np.searchsorted(idx.values, np.datetime64(start)))
    last = min(n - 2, int(np.searchsorted(idx.values, np.datetime64(end), side="right")) - 2)
    if first < 0 or last < first:
        return dict(empty=True)
    live = [a for a in ASSETS if np.isfinite(book.opens[a][first]) and book.opens[a][first] > 0]
    if not live:
        return dict(empty=True)
    w = {a: 0.0 for a in ASSETS}
    for a in live:
        w[a] = 1.0 / len(live)
    eq = START_EQUITY * (1.0 - (rt_cost / 2.0) * sum(w.values()))
    rets, added_sol = [], "SOL" in live
    for t in range(first, last + 1):
        if (not added_sol) and np.isfinite(book.opens["SOL"][t]) and book.opens["SOL"][t] > 0:
            new = {a: 1.0 / 3.0 for a in ASSETS}
            eq *= 1.0 - (rt_cost / 2.0) * sum(abs(new[a] - w[a]) for a in ASSETS)
            w, added_sol = new, True
        r = sum(w[a] * (book.asset_ret[a][t] if np.isfinite(book.asset_ret[a][t]) else 0.0) for a in ASSETS)
        eq *= 1.0 + r
        rets.append(r)
    r = np.asarray(rets)
    eq_path = START_EQUITY * np.cumprod(1.0 + r)
    return dict(empty=False, n_days=len(r), start=str(idx[first].date()), end=str(idx[last + 1].date()),
                final_eq=float(eq), tot_ret=float(eq / START_EQUITY - 1.0), cagr=cagr(eq_path, len(r)),
                vol=float(r.std(ddof=1) * math.sqrt(ANN)) if len(r) > 1 else float("nan"),
                sharpe=sharpe(r), maxdd=max_drawdown(eq_path))

def permute_ohlc(o, h, l, c, rng):
    n = len(c)
    if n < 3:
        return o.copy(), h.copy(), l.copy(), c.copy()
    prev = c[:-1]
    pack = (o[1:] / prev, h[1:] / prev, l[1:] / prev, c[1:] / prev)
    order = rng.permutation(n - 1)
    no, nh, nl, nc = np.empty(n), np.empty(n), np.empty(n), np.empty(n)
    no[0], nh[0], nl[0], nc[0] = o[0], h[0], l[0], c[0]
    px = c[0]
    for i, j in enumerate(order):
        no[i + 1] = px * pack[0][j]
        nh[i + 1] = px * pack[1][j]
        nl[i + 1] = px * pack[2][j]
        nc[i + 1] = px * pack[3][j]
        px = nc[i + 1]
    return no, nh, nl, nc

def decided_for_variant(v, ohlc):
    decided, ready = {}, {}
    for a, df in ohlc.items():
        c, h, l = df["c"].to_numpy(), df["h"].to_numpy(), df["l"].to_numpy()
        sig = vol_ann(c)
        kind = v["kind"]
        if kind == "combo":
            applied, any_long, _ = combo_target(c, sig, v["cap"], v["use_vol"], v["thresh"])
            start_i = 359
            if v["use_vol"]:
                start_i = max(start_i, 90)
            rdy = np.zeros(len(c), dtype=bool)
            rdy[start_i:] = np.isfinite(c[start_i:])
            decided[a], ready[a] = applied, rdy
        elif kind == "sma":
            p = sma_pos(c, v["n"])
            w = size_from_pos(p, sig, v["cap"], v["use_vol"])
            pos_change = np.zeros(len(c), dtype=bool)
            pos_change[0] = True
            pos_change[1:] = p[1:] != p[:-1]
            if v["use_vol"] and v["thresh"] is not None:
                w = apply_rebalance_threshold(w, pos_change, v["thresh"])
            decided[a] = w
            start_i = v["n"] - 1
            if v["use_vol"]:
                start_i = max(start_i, 90)
            rdy = np.zeros(len(c), dtype=bool)
            rdy[start_i:] = np.isfinite(c[start_i:])
            ready[a] = rdy
        else:
            p = donchian_hl_pos(h, l, c, v["n_in"], v["n_out"])
            w = size_from_pos(p, sig, v["cap"], v["use_vol"])
            pos_change = np.zeros(len(c), dtype=bool)
            pos_change[0] = True
            pos_change[1:] = p[1:] != p[:-1]
            if v["use_vol"] and v["thresh"] is not None:
                w = apply_rebalance_threshold(w, pos_change, v["thresh"])
            decided[a] = w
            start_i = max(v["n_in"], v["n_out"]) - 1
            if v["use_vol"]:
                start_i = max(start_i, 90)
            rdy = np.zeros(len(c), dtype=bool)
            rdy[start_i:] = np.isfinite(c[start_i:])
            ready[a] = rdy
    return decided, ready

def btc_only_paper_replication(btc_df):
    c = btc_df["c"].to_numpy()
    idx = btc_df.index
    applied, _, _ = combo_target(c, vol_ann(c), cap=2.0, use_vol=True, thresh=REBAL_THRESH)
    ret = btc_df["c"].pct_change().to_numpy()
    rt, n, start_i = COSTS["10bps"], len(c), 360
    port, w_prev = np.zeros(n), 0.0
    for t in range(start_i, n):
        w = applied[t - 1] if np.isfinite(applied[t - 1]) else 0.0
        r = ret[t] if np.isfinite(ret[t]) else 0.0
        port[t] = w * r - (rt / 2.0) * abs(w - w_prev)
        w_prev = w
    r = port[start_i:]
    eq = START_EQUITY * np.cumprod(1.0 + r)
    return dict(start=str(idx[start_i].date()), end=str(idx[-1].date()), n_days=int(len(r)),
                cagr=cagr(eq, len(r)), vol=float(r.std(ddof=1) * math.sqrt(ANN)),
                sharpe=sharpe(r), maxdd=max_drawdown(eq),
                tot_ret=float(eq[-1] / START_EQUITY - 1.0), final_eq=float(eq[-1]))

def passes_bar(m, folds) -> dict:
    n = m.get("tr_n", 0) or 0
    pf = m.get("tr_pf", float("nan"))
    pos_folds = sum(1 for f in folds if f.get("tot_ret", 0) > 0)
    n_folds = len(folds)
    majority = n_folds > 0 and pos_folds > n_folds / 2.0
    pf_ok = (pf is not None) and (not (isinstance(pf, float) and math.isnan(pf))) and pf >= 1.10
    cash_ok = (m.get("tot_ret", 0) or 0) > 0
    return dict(ok=(n >= 30) and pf_ok and majority and cash_ok, n_ok=n >= 30, pf_ok=bool(pf_ok),
                majority=majority, pos_folds=pos_folds, n_folds=n_folds, cash_ok=cash_ok)

def json_safe(obj):
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        x = float(obj)
        return None if (math.isnan(x) or math.isinf(x)) else x
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, pd.Timestamp):
        return str(obj)
    return obj

def fmt_pct(x, digits=1):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "—"
    return f"{100 * x:.{digits}f}%"

def fmt_num(x, digits=2):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "—"
    return f"{x:.{digits}f}"

def fmt_pf(x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    if isinstance(x, float) and math.isinf(x):
        return "inf"
    return f"{x:.2f}"

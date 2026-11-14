"""Shared research harness: costs, event backtests, purged walk-forward, deflated Sharpe."""
import math, json, os
import numpy as np, pandas as pd
from statistics import NormalDist
N01 = NormalDist()
from load import panel, COINS
DEV_END = pd.Timestamp("2026-10-01", tz="UTC")       # dev = 2023-01-01 .. 2026-09-30
HOLD_END = pd.Timestamp("2026-11-14", tz="UTC")      # holdout = 2026-10-01 .. 2026-11-13 (untouched until final)
EMBARGO = pd.Timedelta("72h")
# round-trip cost in return units
COSTS = {
    "bus_base": 2 * (2 + 3) / 1e4,    # Binance.US taker 2 bps + 3 bps slip per side = 10 bps RT
    "bus_stress": 2 * (2 + 5) / 1e4,  # 14 bps RT
    "krk_maker": 2 * 40 / 1e4,        # Kraken tier-1 maker both sides, no slip = 80 bps RT (best case)
    "krk_taker": 2 * (80 + 5) / 1e4,  # Kraken taker 80 + 5 slip per side = 170 bps RT
}
FOLDS = pd.period_range("2023Q1", "2026Q3", freq="Q")
def quarter(ts): return pd.PeriodIndex(pd.DatetimeIndex(ts).tz_localize(None), freq="Q")

def simulate(sig, k1h, hold, stop=None, target=None):
    """sig: bool Series on 1h bar index (True = signal on CLOSED bar t). Entry at open[t+1],
    time exit at open[t+1+hold]; optional stop/target fractions checked on 1h high/low (stop first).
    Non-overlapping per coin. Returns DataFrame of trades (gross return)."""
    o, h, l = k1h["o"].values, k1h["h"].values, k1h["l"].values
    idx = k1h.index; s = sig.reindex(idx, fill_value=False).values
    out = []; i = 0; n = len(idx)
    while i < n - hold - 2:
        if not s[i]: i += 1; continue
        e = i + 1; px = o[e]; xi = e + hold; xp = o[xi]; why = "time"
        if stop is not None or target is not None:
            for j in range(e, e + hold):
                if stop is not None and l[j] <= px * (1 - stop): xi, xp, why = j, px * (1 - stop), "stop"; break
                if target is not None and h[j] >= px * (1 + target): xi, xp, why = j, px * (1 + target), "tgt"; break
        out.append((idx[e], idx[min(xi, n - 1)], px, xp, why))
        i = xi if why != "time" else xi - 1
        i += 1
    t = pd.DataFrame(out, columns=["entry", "exit", "px", "xp", "why"])
    t["gross"] = t["xp"] / t["px"] - 1
    return t

def stats_of(r):
    r = np.asarray(r, float)
    if len(r) == 0: return dict(n=0, mean_bps=np.nan, pf=np.nan, win=np.nan, sr=np.nan, t=np.nan)
    gp, gl = r[r > 0].sum(), -r[r < 0].sum()
    sd = r.std(ddof=1) if len(r) > 1 else np.nan
    return dict(n=len(r), mean_bps=1e4 * r.mean(), pf=(gp / gl if gl > 0 else np.inf), win=(r > 0).mean(),
                sr=(r.mean() / sd if sd and sd > 0 else np.nan), t=(r.mean() / sd * math.sqrt(len(r)) if sd and sd > 0 else np.nan))

def evaluate(trades, label="", base_mean=None):
    """trades: DataFrame with entry, gross (pooled across coins). Dev-only folds by entry quarter."""
    t = trades[(trades.entry < DEV_END)].copy()
    res = {"label": label, **{f"gross_{k}": v for k, v in stats_of(t.gross).items() if k in ("n", "mean_bps", "pf", "win", "t")}}
    if base_mean is not None: res["excess_bps"] = res["gross_mean_bps"] - 1e4 * base_mean
    q = quarter(t.entry)
    for cn, c in COSTS.items():
        net = t.gross - c; s = stats_of(net)
        fs = net.groupby(q).sum(); fs = fs.reindex(FOLDS).dropna()
        res[f"{cn}_pf"] = s["pf"]; res[f"{cn}_mean_bps"] = s["mean_bps"]
        res[f"{cn}_folds"] = f"{int((fs > 0).sum())}/{len(fs)}"
    return res

def passes(r, cost="bus_base"):
    n = r["gross_n"]; fp, ft = map(int, r[f"{cost}_folds"].split("/"))
    return n >= 30 and r[f"{cost}_pf"] >= 1.10 and fp > ft / 2

def walk_forward(variant_trades, cost, train_q=4, min_train=10):
    """variant_trades: dict name -> trades df. For each test quarter (from the 5th), pick the
    variant with best mean net on the previous train_q quarters (trades whose EXIT is before
    test start - embargo), then take its trades entered in the test quarter. Returns OOS trades + picks."""
    c = COSTS[cost]; oos = []; picks = []
    for k in range(train_q, len(FOLDS)):
        ts, te = FOLDS[k].start_time.tz_localize("UTC"), FOLDS[k].end_time.tz_localize("UTC")
        tr0 = FOLDS[k - train_q].start_time.tz_localize("UTC")
        best, bv = None, -np.inf
        for name, t in variant_trades.items():
            m = t[(t.entry >= tr0) & (t.exit < ts - EMBARGO)]
            if len(m) < min_train: continue
            v = (m.gross - c).mean()
            if v > bv: best, bv = name, v
        picks.append((str(FOLDS[k]), best, None if best is None else round(1e4 * bv, 1)))
        if best is None or bv <= 0: continue   # sit out if nothing was positive in train
        t = variant_trades[best]; oos.append(t[(t.entry >= ts) & (t.entry <= te)].assign(variant=best))
    oos = pd.concat(oos) if oos else pd.DataFrame(columns=["entry", "exit", "gross", "variant"])
    return oos, picks

def wf_summary(oos, cost):
    c = COSTS[cost]; net = oos.gross - c; s = stats_of(net)
    q = quarter(oos.entry) if len(oos) else []
    fs = net.groupby(q).sum() if len(oos) else pd.Series(dtype=float)
    tested = FOLDS[4:]
    return dict(n=s["n"], mean_bps=s["mean_bps"], pf=s["pf"], win=s["win"], t=s["t"],
                folds=f"{int((fs > 0).sum())}/{len(tested)}", traded_folds=len(fs))

def deflated_sharpe(r, n_trials, sr_var):
    """Bailey & Lopez de Prado DSR on per-trade returns. sr_var = variance of per-trade SR across trials."""
    r = np.asarray(r, float); T = len(r)
    if T < 3: return np.nan
    sr = r.mean() / r.std(ddof=1); d = r - r.mean(); m2 = (d**2).mean(); g3 = (d**3).mean() / m2**1.5; g4 = (d**4).mean() / m2**2
    em = 0.5772156649
    sr0 = math.sqrt(sr_var) * ((1 - em) * N01.inv_cdf(1 - 1 / n_trials) + em * N01.inv_cdf(1 - 1 / (n_trials * math.e)))
    z = (sr - sr0) * math.sqrt(T - 1) / math.sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr ** 2)
    return float(N01.cdf(z))

def base_fwd(k1h, hold, mask=None):
    """Unconditional mean open-to-open forward return over `hold` bars (random-entry baseline), dev only."""
    o = k1h["o"]; f = (o.shift(-(hold + 1)) / o.shift(-1) - 1)
    f = f[f.index < DEV_END]
    return f.mean()

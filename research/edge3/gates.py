"""Round-2 extra gates (parent steering, 2026-11-14), applied to every final candidate incl. CAPIT-24:
 (1) random-entry permutation test: 1,000 runs, same coin mix, same holding lengths, entries drawn uniformly from
     the same evaluation window; p = share of runs with mean net >= strategy mean net; must be p < 0.01.
 (2) buy-and-hold: strategy return must beat an equal-weight buy-and-hold of the same 12 coins over the same window,
     after one round-trip fee for B&H (reported for the full dev window, each year, and the holdout).
 (3) regime split, pre-specified, no tuning, DAILY bars per coin, label known at the prior daily close:
       raw = 'flat' if ADX14 < 20; else 'bull' if close > EMA200 and EMA200 rising (vs prior day);
             else 'bear' if close < EMA200 and EMA200 falling; else 'flat'.
     A new label takes effect only after the raw label has held 3 consecutive daily bars."""
import glob, zipfile, io, numpy as np, pandas as pd
from r2common import *
def _daily(c):
    fs = sorted(glob.glob(f"/workspace/edge2-data/spot1d/{c}USDT-1d-*.zip")); out = []
    for f in fs:
        z = zipfile.ZipFile(f); df = pd.read_csv(io.BytesIO(z.read(z.namelist()[0])), header=None).iloc[:, :6]; out.append(df)
    df = pd.concat(out); t = df[0].astype("int64"); t = np.where(t > 1e14, t // 1000, t)
    d = pd.DataFrame(df[[1, 2, 3, 4]].values.astype(float), columns=list("ohlc"), index=pd.to_datetime(t, unit="ms", utc=True))
    return d[~d.index.duplicated()].sort_index()
def adx(d, n=14):
    up = d.h.diff(); dn = -d.l.diff()
    pdm = np.where((up > dn) & (up > 0), up, 0.0); ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = np.maximum(d.h - d.l, np.maximum((d.h - d.c.shift()).abs(), (d.l - d.c.shift()).abs()))
    a = lambda x: pd.Series(x, index=d.index).ewm(alpha=1 / n, adjust=False).mean()
    atr = a(tr); pdi = 100 * a(pdm) / atr; ndi = 100 * a(ndm) / atr
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi); return dx.ewm(alpha=1 / n, adjust=False).mean()
def regime_daily(c):
    d = _daily(c); e = d.c.ewm(span=200, adjust=False).mean(); ax = adx(d)
    raw = np.where(ax < 20, "flat", np.where((d.c > e) & (e.diff() > 0), "bull", np.where((d.c < e) & (e.diff() < 0), "bear", "flat")))
    lab, cur, run, prev = [], "flat", 0, None
    for r in raw:
        run = run + 1 if r == prev else 1; prev = r
        if r != cur and run >= 3: cur = r
        lab.append(cur)
    s = pd.Series(lab, index=d.index); s[d.index < d.index[0] + pd.Timedelta(days=200)] = "warmup"
    return s
_REG = {}
def regime_at(c, ts):
    """Label in force at timestamp ts = label computed on the last daily bar that CLOSED before ts."""
    if c not in _REG: _REG[c] = regime_daily(c)
    s = _REG[c]; s2 = s.copy(); s2.index = s.index + pd.Timedelta("1D")   # bar opened D closes at D+1
    return s2.reindex(pd.DatetimeIndex(ts), method="ffill").values
def regime_split(t, costs=("bus_base", "krk_maker")):
    t = t.copy(); t["regime"] = [regime_at(c, [e])[0] for c, e in zip(t.coin, t.entry)]
    out = {}
    for g, s in t.groupby("regime"):
        out[g] = {"n": len(s), **{cn: dict(pf=round(stats_of(s.gross - COSTS[cn])["pf"], 2), mean_bps=round(stats_of(s.gross - COSTS[cn])["mean_bps"], 1)) for cn in costs}}
    return out
def permutation_test(t, start, end, cost="bus_base", n_runs=1000, seed=7):
    P = panel(); rng = np.random.default_rng(seed); c = COSTS[cost]
    actual = (t.gross - c).mean(); holds = ((t.exit - t.entry) / pd.Timedelta("1h")).round().astype(int).values
    opens = {k: P["k1h"][k]["o"] for k in set(t.coin)}
    cand = {k: np.where((opens[k].index >= start) & (opens[k].index < end))[0] for k in opens}
    sims = np.empty(n_runs)
    for r in range(n_runs):
        g = []
        for coin, h in zip(t.coin, holds):
            ix = cand[coin]; ix = ix[ix + h < len(opens[coin])]; i = rng.choice(ix)
            g.append(opens[coin].iloc[i + h] / opens[coin].iloc[i] - 1 - c)
        sims[r] = np.mean(g)
    return dict(actual_mean_bps=round(1e4 * actual, 1), null_mean_bps=round(1e4 * sims.mean(), 1), null_p99_bps=round(1e4 * np.quantile(sims, .99), 1),
                p_value=float((np.sum(sims >= actual) + 1) / (n_runs + 1)), runs=n_runs)
def buy_hold(start, end, cost="bus_base"):
    P = panel(); r = []
    for c in COINS:
        o = P["k1h"][c]["o"]; o = o[(o.index >= start) & (o.index < end)]
        if len(o) > 1: r.append(o.iloc[-1] / o.iloc[0] - 1 - COSTS[cost])
    return float(np.mean(r))
def strat_return(t, start, end, cost="bus_base", slot=0.5):
    s = t[(t.entry >= start) & (t.entry < end)]; return float((slot * (s.gross - COSTS[cost])).sum())
def bh_table(t, cost="bus_base", slot=0.5, hold_end=HOLD_END):
    wins = [("dev", pd.Timestamp("2023-02-01", tz="UTC"), DEV_END)] + \
           [(str(y), pd.Timestamp(f"{y}-01-01", tz="UTC") if y > 2023 else pd.Timestamp("2023-02-01", tz="UTC"), min(pd.Timestamp(f"{y+1}-01-01", tz="UTC"), DEV_END)) for y in [2023, 2024, 2025, 2026]] + \
           [("holdout", DEV_END, hold_end)]
    return {w: dict(strat_pct=round(100 * strat_return(t, a, b, cost, slot), 1), bh_pct=round(100 * buy_hold(a, b, cost), 1),
                    beats=strat_return(t, a, b, cost, slot) > buy_hold(a, b, cost)) for w, a, b in wins}

"""Edge hunt 2026-11-16: quick, fee-aware, pre-specified checks of the long-horizon candidates.
Data: Binance Vision spot 1d USDT klines (monthly zips) 2018-01..2026-10 for 20 large coins, cached at
/workspace/edge2-data/xs1d (outside the repo). Survivorship: today's large coins (biased upward for XS tests).
Execution model: signal on day t close, position held on day t+1 (close-to-close), costs charged on |dw|.
Costs (round trip): bus 10 bps (Binance.US 0.02% taker + 3 bps slip/side, conservative; maker would be ~0),
krk 80 bps (Kraken tier-1 maker both sides), krk_t 160 bps (Kraken tier-1 taker both sides).
Walk-forward: yearly test folds; each fold uses the variant with the best net Sharpe over the prior 2 years.
Every variant family below was written down before running. Nothing tuned afterwards."""
import glob, io, json, math, zipfile, sys
import numpy as np, pandas as pd
from statistics import NormalDist
N01 = NormalDist()
D = "/workspace/edge2-data/xs1d"
COINS = "BTC ETH BNB XRP ADA DOGE SOL DOT LTC LINK AVAX TRX XLM ALGO VET HBAR ATOM ETC BCH UNI".split()
COSTS = {"bus10": 10e-4, "krk80": 80e-4, "krk160": 160e-4}
PRIOR_N = 529
def load(c):
    out = []
    for f in sorted(glob.glob(f"{D}/{c}USDT-1d-*.zip")):
        z = zipfile.ZipFile(f); out.append(pd.read_csv(io.BytesIO(z.read(z.namelist()[0])), header=None).iloc[:, :6])
    if not out: return None
    d = pd.concat(out); t = d[0].astype("int64"); t = np.where(t > 1e14, t // 1000, t)
    d = pd.DataFrame(d[[1, 2, 3, 4, 5]].values.astype(float), columns=list("ohlcv"), index=pd.to_datetime(t, unit="ms", utc=True).normalize())
    return d[~d.index.duplicated()].sort_index()
PX = {c: load(c) for c in COINS}; PX = {c: d for c, d in PX.items() if d is not None}
C = pd.DataFrame({c: d.c for c, d in PX.items()}).sort_index()
R = C.pct_change()
def sharpe(x): x = pd.Series(x).dropna(); return float(x.mean() / x.std() * math.sqrt(365)) if x.std() > 0 else 0.0
def run_weights(W, cost):
    """W: target weights decided at close t (rows) -> held over day t+1."""
    W = W.fillna(0.0); held = W.shift(1).fillna(0.0)
    gross = (held * R[W.columns].fillna(0.0)).sum(axis=1)
    turn = (W - W.shift(1).fillna(0.0)).abs().sum(axis=1).shift(1).fillna(0.0)
    return gross - turn * cost / 2
def stats(r, W=None):
    r = r.dropna(); eq = (1 + r).cumprod(); dd = float((eq / eq.cummax() - 1).min())
    yrs = len(r) / 365; cagr = float(eq.iloc[-1] ** (1 / yrs) - 1) if yrs > 0 else np.nan
    return dict(cagr=round(100 * cagr, 1), sharpe=round(sharpe(r), 2), maxdd=round(100 * dd, 1), total=round(100 * (eq.iloc[-1] - 1), 1))
def trades_pf(W, cost, col):
    """Per-trade PF for a single-asset 0/1-ish sleeve: a trade = contiguous stretch with weight > 0."""
    w = W[col].fillna(0.0); held = w.shift(1).fillna(0.0); r = R[col].fillna(0.0)
    on = held > 0; grp = (on != on.shift()).cumsum()
    pn = []
    for g, idx in on.groupby(grp):
        if not idx.iloc[0]: continue
        sl = idx.index; pn.append(float((1 + held.loc[sl] * r.loc[sl]).prod() - 1 - cost * held.loc[sl].mean()))
    pn = np.array(pn); gp, gl = pn[pn > 0].sum(), -pn[pn < 0].sum()
    return len(pn), (round(gp / gl, 2) if gl > 0 else None), round(100 * (pn > 0).mean(), 1) if len(pn) else None
def walk_forward(fam, cost, years, train_y=2):
    """fam: name -> W. Returns OOS daily returns, picks."""
    rets = {k: run_weights(W, cost) for k, W in fam.items()}; oos = []; picks = {}
    for y in years:
        a, b = pd.Timestamp(f"{y - train_y}-01-01", tz="UTC"), pd.Timestamp(f"{y}-01-01", tz="UTC")
        e = pd.Timestamp(f"{y + 1}-01-01", tz="UTC")
        best = max(rets, key=lambda k: sharpe(rets[k][(rets[k].index >= a) & (rets[k].index < b)]))
        picks[y] = best; oos.append(rets[best][(rets[best].index >= b) & (rets[best].index < e)])
    return pd.concat(oos), picks, rets
def dsr(r, n_trials, sr_var):
    r = np.asarray(pd.Series(r).dropna(), float); T = len(r); sr = r.mean() / r.std(ddof=1)
    d = r - r.mean(); m2 = (d ** 2).mean(); g3 = (d ** 3).mean() / m2 ** 1.5; g4 = (d ** 4).mean() / m2 ** 2; em = 0.5772156649
    sr0 = math.sqrt(sr_var) * ((1 - em) * N01.inv_cdf(1 - 1 / n_trials) + em * N01.inv_cdf(1 - 1 / (n_trials * math.e)))
    return float(N01.cdf((sr - sr0) * math.sqrt(T - 1) / math.sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr ** 2)))
def perm_p(Woos_fn, cost, n=500, seed=3):
    """circular-shift test: shift the OOS weight path in time vs returns; p = share with Sharpe >= actual."""
    W, mask = Woos_fn; rng = np.random.default_rng(seed); act = sharpe(run_weights(W, cost)[mask])
    sims = []
    for _ in range(n):
        k = int(rng.integers(30, len(W) - 30)); Ws = pd.DataFrame(np.roll(W.values, k, axis=0), index=W.index, columns=W.columns)
        sims.append(sharpe(run_weights(Ws, cost)[mask]))
    return round((np.sum(np.array(sims) >= act) + 1) / (n + 1), 4)
def yearly(r): return {int(y): round(100 * float((1 + g).prod() - 1), 1) for y, g in r.groupby(r.index.year)}
OUT = {}
# ───────── A. time-series trend, BTC+ETH 50/50 (Liu-Tsyvinski TSMOM; Zarattini-style trend) ─────────
A_COLS = ["BTC", "ETH"]
def famA():
    f = {}
    for n in [50, 100, 200]:
        f[f"sma{n}"] = pd.DataFrame({c: (C[c] > C[c].rolling(n).mean()).astype(float) * 0.5 for c in A_COLS})
    for L in [7, 14, 28]:
        f[f"tsmom{L}"] = pd.DataFrame({c: (C[c].pct_change(L) > 0).astype(float) * 0.5 for c in A_COLS})
    return f
# vol-managed version of each (target 40% ann vol per sleeve, cap 0.5) as a separate family
def famA_vol():
    f = {}; vol = {c: R[c].rolling(30).std() * math.sqrt(365) for c in A_COLS}
    for k, W in famA().items():
        f[k + "_vt"] = pd.DataFrame({c: (W[c] > 0).astype(float) * np.minimum(0.5, 0.5 * 0.40 / vol[c]) for c in A_COLS})
    return f
# ───────── B. cross-sectional momentum, 20 coins, weekly (Liu-Tsyvinski-Wu CMOM, long-only) ─────────
def famB():
    f = {}
    for L in [7, 28]:
        for absf in [False, True]:
            mom = C.pct_change(L); rows = []
            for t in C.index:
                if t.dayofweek != 0: rows.append(None); continue   # rebalance Mondays only
                m = mom.loc[t].dropna(); m = m[C.loc[:t].tail(60).notna().all()[m.index]]   # need 60d history
                top = m.sort_values(ascending=False).head(4)
                if absf: top = top[top > 0]
                rows.append({c: 1 / 4 for c in top.index})
            W = pd.DataFrame([r if r is not None else {} for r in rows], index=C.index, columns=C.columns, dtype=float)
            hold = pd.Series([r is not None for r in rows], index=C.index)
            W = W.where(hold, np.nan); W[hold] = W[hold].fillna(0.0); W = W.ffill().fillna(0.0)
            f[f"xs_top4_L{L}{'_abs' if absf else ''}"] = W
    return f
def bench_ew():
    avail = C.notna() & C.rolling(60).count().ge(60)
    W = avail.astype(float).div(avail.sum(axis=1).replace(0, np.nan), axis=0)
    mon = pd.Series(C.index.dayofweek == 0, index=C.index)
    return W.where(mon).ffill().fillna(0.0)
def bh(cols, w=None):
    w = w or {c: 1 / len(cols) for c in cols}
    return pd.DataFrame({c: pd.Series(w[c], index=C.index).where(C[c].notna(), 0.0) for c in cols})
if __name__ == "__main__":
    years = list(range(2020, 2027))
    fams = {"A_trend_btc_eth": famA(), "A_trend_btc_eth_volmanaged": famA_vol(), "B_xs_mom_20coins": famB()}
    n_new = sum(len(f) for f in fams.values()); NT = PRIOR_N + n_new
    for fname, fam in fams.items():
        res = {}
        allsr = [sharpe(run_weights(W, COSTS["bus10"])[C.index >= pd.Timestamp("2019-01-01", tz="UTC")]) / math.sqrt(365) for W in fam.values()]
        srv = float(np.var(allsr)) if len(allsr) > 1 else 1e-4
        for cn, cost in COSTS.items():
            oos, picks, rets = walk_forward(fam, cost, years)
            Woos = pd.DataFrame(0.0, index=C.index, columns=C.columns)
            for y, k in picks.items():
                m = (C.index >= pd.Timestamp(f"{y}-01-01", tz="UTC")) & (C.index < pd.Timestamp(f"{y + 1}-01-01", tz="UTC"))
                W = fam[k].reindex(columns=C.columns).fillna(0.0); Woos.loc[m] = W.loc[m].values
            mask = (C.index >= pd.Timestamp("2020-01-01", tz="UTC"))
            cols = A_COLS if fname.startswith("A") else list(C.columns)
            bench = run_weights(bh(cols) if fname.startswith("A") else bench_ew(), cost)[mask]
            yr = yearly(oos); byr = yearly(bench)
            res[cn] = dict(oos=stats(oos), bench=stats(bench), folds_pos=f"{sum(v > 0 for v in yr.values())}/{len(yr)}",
                           beats_bench_years=f"{sum(yr[y] > byr.get(y, -1e9) for y in yr)}/{len(yr)}", oos_by_year=yr, bench_by_year=byr,
                           picks=picks, perm_p=perm_p((Woos, mask), cost) if cn != "krk160" else None,
                           dsr=round(dsr(oos.values, NT, srv), 3), exposure=round(float((Woos.shift(1).loc[mask].sum(axis=1)).mean()), 2),
                           turnover_per_year=round(float((Woos - Woos.shift(1)).abs().sum(axis=1).loc[mask].sum() / (mask.sum() / 365)), 1))
            if fname.startswith("A"):
                res[cn]["trades_pf_win_btc"] = trades_pf(Woos.loc[mask], cost, "BTC")
            print(fname, cn, json.dumps({k: v for k, v in res[cn].items() if k not in ("bench_by_year",)}), flush=True)
        OUT[fname] = res
    # ───────── C. weekend / day-of-week, BTC ─────────
    b = R["BTC"].dropna(); b = b[b.index >= "2018-01-01"]
    wk = {}
    for y, g in b.groupby(b.index.year):
        we = g[g.index.dayofweek >= 5]; wd = g[g.index.dayofweek < 5]
        wk[int(y)] = dict(weekend_bps=round(1e4 * we.mean(), 1), weekday_bps=round(1e4 * wd.mean(), 1))
    we = b[b.index.dayofweek >= 5]; wd = b[b.index.dayofweek < 5]
    t = (we.mean() - wd.mean()) / math.sqrt(we.var() / len(we) + wd.var() / len(wd))
    OUT["C_weekend_btc"] = dict(by_year=wk, all=dict(weekend_bps=round(1e4 * we.mean(), 1), weekday_bps=round(1e4 * wd.mean(), 1), welch_t=round(t, 2)))
    print("C", json.dumps(OUT["C_weekend_btc"]))
    # ───────── D. funding carry yield (Binance USDT-M funding, proxy for Coinbase/Kraken US perps) ─────────
    sys.path.insert(0, "/workspace/mayo-bot/research/edge2")
    from load import panel
    P = panel(); fy = {}
    for c in ["BTC", "ETH", "SOL", "XRP"]:
        f = P["fund"][c]; f = f[~f.index.duplicated()]
        fy[c] = {int(y): dict(ann_pct=round(100 * float(g.sum()) * 365 / max(1, (g.index.max() - g.index.min()).days + 1), 2), neg_share=round(float((g < 0).mean()), 2)) for y, g in f.groupby(f.index.year)}
    OUT["D_funding_carry"] = fy; print("D", json.dumps(fy))
    json.dump(OUT, open("/workspace/mayo-bot/research/edge5/hunt.json", "w"), indent=1, default=str)

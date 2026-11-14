"""Idea 5: independent re-verification of Ledger's SOL 1h DualThrust (long-only, BTC-band filter,
band/stop exit), with our own code and data (15m execution bars instead of 5m, 2023-01 onward).
Bands per Ledger spec: rng = max(HH-LC, HC-LL) over prior N 1h bars; up = open + k1*km*rng,
km = 0.7 + 0.6*pct-rank(ATR14/price, 500 bars, lagged). Entry on 15m touch of up (fill max(open, up));
BTC filter: previous 15m BTC close > BTC's own up band. Stop 1xATR14(1h); exit on touch of lo band.
Same-bar stop counted as hit. Per-trade returns at unit notional; stop_frac kept for risk-weighting."""
import pickle, itertools, numpy as np, pandas as pd
from numba import njit
from common import *
P = panel()
def bands(k1h, N, k1, k2):
    r = k1h; HH = r.h.rolling(N).max().shift(1); LL = r.l.rolling(N).min().shift(1)
    HC = r.c.rolling(N).max().shift(1); LC = r.c.rolling(N).min().shift(1); rng = np.maximum(HH - LC, HC - LL)
    tr = np.maximum(r.h - r.l, np.maximum((r.h - r.c.shift()).abs(), (r.l - r.c.shift()).abs()))
    atr = tr.rolling(14).mean().shift(1); pct = (atr / r.o).rolling(500, min_periods=100).rank(pct=True)
    km = 0.7 + 0.6 * pct.fillna(.5)
    return pd.DataFrame({"up": r.o + k1 * km * rng, "lo": r.o - k2 * km * rng, "atr": atr, "bar": np.arange(len(r))}, index=r.index)
@njit(cache=True)
def sim(o, h, l, up, lo, atr, bar, fl):
    n = len(o); pos = 0; ent = 0.; stp = 0.; sd0 = 0.; lastbar = -1; ei = 0
    res = np.zeros((n, 4)); k = 0
    for i in range(1, n):
        if np.isnan(up[i]) or np.isnan(atr[i]): continue
        if pos == 1:
            ex = -1.
            if l[i] <= stp: ex = min(o[i], stp)
            elif l[i] <= lo[i]: ex = min(o[i], lo[i])
            if ex > 0:
                res[k, 0] = ei; res[k, 1] = i; res[k, 2] = ex / ent - 1; res[k, 3] = sd0 / ent; k += 1; pos = 0
        if pos == 0 and bar[i] != lastbar and h[i] >= up[i] and fl[i]:
            ent = max(o[i], up[i]); sd0 = atr[i]; stp = ent - sd0; pos = 1; ei = i; lastbar = bar[i]
            if l[i] <= stp:
                res[k, 0] = ei; res[k, 1] = i; res[k, 2] = stp / ent - 1; res[k, 3] = sd0 / ent; k += 1; pos = 0
    return res[:k]
def run(sym, N, k1, k2, filt=True):
    x15 = P["k15"][sym]; b15 = P["k15"]["BTC"].reindex(x15.index)
    S = bands(P["k1h"][sym], N, k1, k2).reindex(x15.index, method="ffill")
    B = bands(P["k1h"]["BTC"], N, k1, k2).reindex(x15.index, method="ffill")
    fl = (b15.c.shift(1) > B.up).values if filt else np.ones(len(x15), bool)
    r = sim(x15.o.values, x15.h.values, x15.l.values, S.up.values, S.lo.values, S.atr.values, S.bar.fillna(-1).values.astype(np.int64), fl)
    ix = x15.index
    return pd.DataFrame({"entry": ix[r[:, 0].astype(int)], "exit": ix[r[:, 1].astype(int)], "gross": r[:, 2], "stop_frac": r[:, 3], "coin": sym})
GRID = list(itertools.product([3, 6, 12, 24], [.3, .5, .8, 1.2], [.3, .5, .8, 1.2]))
if __name__ == "__main__":
    reg = {}
    for sym in ["SOL", "ETH"]:
        for g in GRID: reg[f"F5|{sym}|N{g[0]}|k{g[1]}|{g[2]}"] = run(sym, *g)
    pickle.dump(reg, open("/workspace/edge2-data/reg_idea5.pkl", "wb"))
    rows = [evaluate(t, n) for n, t in reg.items()]
    df = pd.DataFrame(rows); df.to_csv("out_idea5.csv", index=False)
    pd.set_option("display.width", 250)
    print(df.sort_values("bus_base_pf", ascending=False)[["label", "gross_n", "gross_mean_bps", "gross_win", "bus_base_pf", "bus_base_folds", "krk_maker_pf", "krk_maker_folds"]].head(12).round(3).to_string())
    print("share of SOL variants with bus_base PF>=1.10:", (df[df.label.str.contains('SOL')].bus_base_pf >= 1.1).mean())
    print("frozen paper spec:"); print(df[df.label == "F5|SOL|N12|k0.8|0.8"].T.to_string())

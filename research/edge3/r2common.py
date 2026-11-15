"""Round-2 harness: reuses research/edge2 (panel, costs, simulate, evaluate, purged WF, DSR)."""
import sys, os
sys.path.insert(0, "/workspace/mayo-bot/research/edge2")
from common import *            # noqa
from features import rq         # noqa
import numpy as np, pandas as pd, pickle
ROUND1_VARIANTS = 378
def run_family(name_fn_sig, k_by_coin, hold, coins=COINS, bars_per_hour=1):
    tr, bm = [], []
    for c in coins:
        k = k_by_coin[c]; sig = name_fn_sig(c)
        t = simulate(sig.fillna(False), k, hold); t["coin"] = c; tr.append(t)
        o = k["o"]; f = (o.shift(-(hold + 1)) / o.shift(-1) - 1); bm.append(f[f.index < DEV_END].mean())
    return pd.concat(tr, ignore_index=True), float(np.mean(bm))
def day_clustered_t(t, cost=0.0):
    d = t[t.entry < DEV_END]
    if len(d) < 3: return np.nan, 0
    g = (d.gross - cost).groupby(d.entry.dt.floor("D")).mean()
    return (g.mean() / g.std() * np.sqrt(len(g)) if g.std() > 0 else np.nan), len(g)
def show(df, cols=None):
    pd.set_option("display.width", 260); pd.set_option("display.max_rows", 400)
    cols = cols or ["label", "gross_n", "gross_mean_bps", "excess_bps", "dc_t", "bus_base_pf", "bus_base_folds", "krk_maker_pf", "krk_maker_folds"]
    print(df[cols].round(2).to_string())

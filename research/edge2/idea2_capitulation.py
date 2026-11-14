"""Idea 2: post-capitulation bounce. Big, fast, vol-normalised drop (+ volume spike, + optional
negative-basis 'liquidation' confirmation) -> long spot, time exit."""
import pickle, itertools, numpy as np, pandas as pd
from common import *
from features import *
P = panel()
F = {}
for c in COINS:
    k = P["k1h"][c]; r1 = np.log(k.c).diff(); sd = r1.rolling(720, min_periods=360).std().shift(1)
    pm = P["prem"][c].reindex(k.index).rolling(24, min_periods=12).mean()
    F[c] = dict(k=k, r1=r1, sd=sd, lv=np.log(k.c), qv=k.qv, prem_lo=pm <= rq(pm, 0.10))
rows, reg = [], {}
for N, z, vm, liq, hold in itertools.product([4, 12, 24], [2.0, 2.5, 3.0], [1.0, 2.0], [False, True], [12, 24, 48]):
    tr, bm = [], []
    for c in COINS:
        f = F[c]; rn = f["lv"].diff(N); zz = rn / (f["sd"] * np.sqrt(N))
        volr = f["qv"].rolling(N).sum() / (f["qv"].rolling(720, min_periods=360).mean().shift(N) * N)
        sig = (zz <= -z) & (volr >= vm)
        if liq: sig &= f["prem_lo"]
        t = simulate(sig, f["k"], hold); t["coin"] = c; tr.append(t); bm.append(base_fwd(f["k"], hold))
    tr = pd.concat(tr, ignore_index=True); name = f"F2|ret{N}h|z{z}|vol{vm}|liq{int(liq)}|h{hold}"
    rows.append(evaluate(tr, name, base_mean=np.mean(bm))); reg[name] = tr
df = pd.DataFrame(rows); df.to_csv("out_idea2.csv", index=False)
pickle.dump(reg, open("/workspace/edge2-data/reg_idea2.pkl", "wb"))
pd.set_option("display.width", 250); pd.set_option("display.max_rows", 300)
print(df[["label", "gross_n", "gross_mean_bps", "excess_bps", "gross_t", "bus_base_pf", "bus_base_folds", "krk_maker_pf", "krk_maker_folds"]].round(2).to_string())

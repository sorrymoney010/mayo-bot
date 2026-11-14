"""Idea 1: funding / basis extremes as a contrarian (or momentum) spot signal. Long-only spot."""
import pickle, json, itertools, numpy as np, pandas as pd
from common import *
from features import *
P = panel()
feats = {}
for c in COINS:
    k = P["k1h"][c]; idx = k.index
    pm = P["prem"][c].reindex(idx)
    feats[c] = pd.DataFrame({"fund": funding_on_bars(P["fund"][c], idx), "fund3": funding_mean3(P["fund"][c], idx),
                             "prem": pm, "prem24": pm.rolling(24, min_periods=12).mean()}, index=idx)
rows, reg = [], {}
for feat, q, hold, side in itertools.product(["fund", "fund3", "prem", "prem24"], [0.02, 0.05, 0.10], [12, 24, 48], ["low", "high"]):
    tr = []; bm = []
    for c in COINS:
        x = feats[c][feat]
        sig = (x <= rq(x, q)) if side == "low" else (x >= rq(x, 1 - q))
        t = simulate(sig, P["k1h"][c], hold); t["coin"] = c; tr.append(t); bm.append(base_fwd(P["k1h"][c], hold))
    tr = pd.concat(tr, ignore_index=True)
    name = f"F1|{feat}|{side}{q}|h{hold}"
    r = evaluate(tr, name, base_mean=np.mean(bm)); rows.append(r); reg[name] = tr
df = pd.DataFrame(rows)
pickle.dump(reg, open("/workspace/edge2-data/reg_idea1.pkl", "wb")); df.to_csv("out_idea1.csv", index=False)
pd.set_option("display.width", 250); pd.set_option("display.max_rows", 200)
print(df[["label", "gross_n", "gross_mean_bps", "excess_bps", "gross_t", "bus_base_pf", "bus_base_folds", "krk_maker_pf", "krk_maker_folds"]].round(2).to_string())

"""Idea 4: time-of-day / day-of-week seasonality. Long from UTC hour a for w hours every day
(and day-of-week 24h holds). Gross edge vs unconditional; stability: first half vs second half of dev."""
import pickle, itertools, numpy as np, pandas as pd
from common import *
P = panel()
rows, reg = [], {}
def run(name, sigf, hold):
    tr, bm = [], []
    for c in COINS:
        k = P["k1h"][c]; sig = pd.Series(sigf(k.index), index=k.index)
        t = simulate(sig, k, hold); t["coin"] = c; tr.append(t); bm.append(base_fwd(k, hold))
    tr = pd.concat(tr, ignore_index=True); r = evaluate(tr, name, base_mean=np.mean(bm))
    d = tr[tr.entry < DEV_END]; h1 = d[d.entry < "2024-11-15"]; h2 = d[d.entry >= "2024-11-15"]
    r["ex_h1_bps"] = 1e4 * (h1.gross.mean() - np.mean(bm)); r["ex_h2_bps"] = 1e4 * (h2.gross.mean() - np.mean(bm))
    rows.append(r); reg[name] = tr
for a, w in itertools.product(range(0, 24, 2), [4, 8, 12]):
    # signal on the bar that CLOSES at hour a -> entry at open of hour a
    run(f"F4|hour{a:02d}|w{w}", lambda ix, a=a: ((ix.hour + 1) % 24 == a), w)
for d in range(7):
    run(f"F4|dow{d}|h24", lambda ix, d=d: ((ix + pd.Timedelta('1h')).dayofweek == d) & ((ix.hour + 1) % 24 == 0), 24)
run("F4|weekend|h48", lambda ix: ((ix + pd.Timedelta('1h')).dayofweek == 5) & ((ix.hour + 1) % 24 == 0), 48)
df = pd.DataFrame(rows); df.to_csv("out_idea4.csv", index=False)
pickle.dump(reg, open("/workspace/edge2-data/reg_idea4.pkl", "wb"))
pd.set_option("display.width", 250); pd.set_option("display.max_rows", 100)
print(df[["label", "gross_n", "gross_mean_bps", "excess_bps", "ex_h1_bps", "ex_h2_bps", "bus_base_pf", "bus_base_folds", "krk_maker_pf", "krk_maker_folds"]].round(2).to_string())

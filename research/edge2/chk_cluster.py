import pickle, numpy as np, pandas as pd
from common import *
reg = pickle.load(open("/workspace/edge2-data/reg_idea2.pkl","rb"))
for name in ["F2|ret24h|z2.5|vol2.0|liq0|h48","F2|ret24h|z2.0|vol2.0|liq0|h24","F2|ret12h|z2.5|vol2.0|liq0|h24","F2|ret4h|z3.0|vol2.0|liq0|h24"]:
    t = reg[name]; t = t[t.entry < DEV_END].copy(); t["day"] = t.entry.dt.floor("D")
    d = t.groupby("day").gross.mean()
    print(name, "trades", len(t), "distinct days", len(d), "day-clustered mean bps %.1f t %.2f" % (1e4*d.mean(), d.mean()/d.std()*np.sqrt(len(d))))
    top = t.groupby("day").gross.agg(["count","mean"]).sort_values("mean", ascending=False)
    print(" top days:", [(str(i.date()), int(r["count"]), round(1e4*r["mean"])) for i, r in top.head(6).iterrows()])
    net = t.gross - 0.001; dd = net.groupby(t.day).sum().sort_values(ascending=False)
    print(" net sum bps", round(1e4*net.sum()), "ex top5 days", round(1e4*dd.iloc[5:].sum()), "ex top10", round(1e4*dd.iloc[10:].sum()))
    print(" by coin:", t.groupby("coin").gross.agg(lambda x: round(1e4*x.mean())).to_dict())
    print(" by year:", t.groupby(t.entry.dt.year).gross.agg(["count", lambda x: round(1e4*x.mean())]).values.tolist())

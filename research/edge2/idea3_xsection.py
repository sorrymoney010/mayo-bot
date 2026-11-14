"""Idea 3: cross-sectional relative strength across 12 liquid coins. At each rebalance (every R h,
aligned to 00:00 UTC) rank by past-L-hour return; hold the top-k (momentum) or bottom-k (reversal)
for R hours. Consecutive holdings of the same coin are merged into one trade (one fee).
Gross edge is measured against the equal-weight basket over the same holding windows."""
import pickle, itertools, numpy as np, pandas as pd
from common import *
P = panel()
C = pd.DataFrame({c: P["k1h"][c]["c"] for c in COINS}).dropna()
O = pd.DataFrame({c: P["k1h"][c]["o"] for c in COINS}).reindex(C.index)
idx = C.index; n = len(idx)
rows, reg = [], {}
for L, R, side, k in itertools.product([4, 24, 72, 168], [12, 24, 72], ["top", "bottom"], [2]):
    reb = [i for i in range(max(L, 1), n - R - 2) if idx[i].hour % 24 == 23 and ((idx[i] + pd.Timedelta("1h")).value // 3_600_000_000_000) % R == 0] if R >= 24 else \
          [i for i in range(max(L, 1), n - R - 2) if (idx[i].hour + 1) % R == 0]
    score = C / C.shift(L) - 1
    held = {}; trades = []; basket = []
    for i in reb:
        s = score.iloc[i].dropna()
        pick = set((s.nlargest(k) if side == "top" else s.nsmallest(k)).index)
        e, x = i + 1, i + 1 + R
        basket.append(O.iloc[x].values / O.iloc[e].values - 1)
        for c in list(held):
            if c not in pick: trades.append((held[c][0], idx[e], held[c][1], O[c].iloc[e], c)); del held[c]
        for c in pick:
            if c not in held: held[c] = (idx[e], O[c].iloc[e])
    t = pd.DataFrame(trades, columns=["entry", "exit", "px", "xp", "coin"]); t["gross"] = t.xp / t.px - 1
    bmean = np.nanmean(np.array(basket)[[idx[i] < DEV_END for i in reb]])
    # excess per holding period (pre-fee) vs basket: compute separately
    name = f"F3|L{L}|R{R}|{side}{k}"
    r = evaluate(t, name)
    hp = []
    for i in reb:
        if idx[i] >= DEV_END: break
        s = score.iloc[i].dropna(); pick = (s.nlargest(k) if side == "top" else s.nsmallest(k)).index
        fr = O.iloc[i + 1 + R] / O.iloc[i + 1] - 1
        hp.append(fr[pick].mean() - fr.mean())
    hp = np.array(hp); r["excess_per_period_bps"] = 1e4 * hp.mean(); r["excess_t"] = hp.mean() / hp.std() * np.sqrt(len(hp))
    r["avg_hold_h"] = ((t.exit - t.entry).dt.total_seconds() / 3600).mean()
    rows.append(r); reg[name] = t; print(name, round(r["excess_per_period_bps"], 1), round(r["excess_t"], 2), flush=True)
df = pd.DataFrame(rows); df.to_csv("out_idea3.csv", index=False)
pickle.dump(reg, open("/workspace/edge2-data/reg_idea3.pkl", "wb"))
pd.set_option("display.width", 250)
print(df[["label", "gross_n", "avg_hold_h", "gross_mean_bps", "excess_per_period_bps", "excess_t", "bus_base_pf", "bus_base_folds", "krk_maker_pf", "krk_maker_folds"]].round(2).to_string())

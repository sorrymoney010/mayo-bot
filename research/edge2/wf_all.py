"""Purged walk-forward per family (train 4 quarters, 72h embargo, quarterly test, sit out when the
best train variant is not positive) + multiple-testing accounting (deflated Sharpe on day-clustered
returns, N = every variant tried). Dev only (holdout untouched)."""
import pickle, json, numpy as np, pandas as pd
from common import *
fams = {i: pickle.load(open(f"/workspace/edge2-data/reg_idea{i}.pkl", "rb")) for i in [1, 2, 3, 4, 5]}
fams["5sol"] = {k: v for k, v in fams[5].items() if "|SOL|" in k}
ALL = {k: v for i in [1, 2, 3, 4, 5] for k, v in fams[i].items()}
NTR = len(ALL)
def clustered(t, cost):
    d = t[t.entry < DEV_END]; return (d.gross - cost).groupby(d.entry.dt.floor("D")).mean()
srs = []
for k, t in ALL.items():
    c = clustered(t, COSTS["bus_base"])
    if len(c) > 10 and c.std() > 0: srs.append(c.mean() / c.std())
SRVAR = float(np.var(srs))
out = {"n_variants": NTR, "per_family": {i: len(fams[i]) for i in [1, 2, 3, 4, 5]}, "sr_var_dayclustered": SRVAR, "wf": {}}
print("variants:", out["per_family"], "total", NTR, "SR var", round(SRVAR, 5))
for fam in [1, 2, 3, 4, 5, "5sol"]:
    for cost in ["bus_base", "krk_maker"]:
        oos, picks = walk_forward(fams[fam], cost)
        s = wf_summary(oos, cost)
        s["stress_pf"] = stats_of(oos.gross - COSTS["bus_stress" if cost == "bus_base" else "krk_taker"])["pf"] if len(oos) else np.nan
        if len(oos):
            dc = (oos.gross - COSTS[cost]).groupby(oos.entry.dt.floor("D")).mean()
            s["dsr"] = deflated_sharpe(dc.values, NTR, SRVAR); s["days"] = len(dc)
            s["y2026_mean_bps"] = 1e4 * (oos[oos.entry >= "2026-01-01"].gross - COSTS[cost]).mean()
            s["y2026_n"] = int((oos.entry >= "2026-01-01").sum())
        s["picks"] = picks
        out["wf"][f"F{fam}|{cost}"] = s
        print(f"F{fam} {cost}: n={s['n']} mean={s['mean_bps']:.1f}bps pf={s['pf']:.2f} folds={s['folds']} stressPF={s['stress_pf']:.2f} dsr={s.get('dsr', np.nan):.3f} 2026: n={s.get('y2026_n')} mean={s.get('y2026_mean_bps', np.nan):.1f}")
        print("   picks:", [p[1] for p in picks])
json.dump(out, open("wf_all.json", "w"), indent=1, default=str)

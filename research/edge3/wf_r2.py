"""Round-2 purged walk-forward per family (same harness as round 1) + deflated Sharpe with the CUMULATIVE
variant count (round 1 + round 2) and SR variance across every variant tried in both rounds."""
import json
from r2common import *
R1 = {}
for i in [1, 2, 3, 4, 5]: R1.update(pickle.load(open(f"/workspace/edge2-data/reg_idea{i}.pkl", "rb")))
fams = {k: pickle.load(open(f"/workspace/edge2-data/reg_{k}.pkl", "rb")) for k in ["c1", "c2", "c4"]}
fams["c1_oi"] = {k: v for k, v in fams["c1"].items() if k.startswith("OI|drop")}
fams["c1_pos"] = {k: v for k, v in fams["c1"].items() if k.startswith("POS|")}
fams["c1_capoi"] = {k: v for k, v in fams["c1"].items() if "capit+oiflush" in k}
ALL = {**R1, **fams["c1"], **fams["c2"], **fams["c4"]}
N_TOTAL = len(ALL) + 2 + 4 + 2   # + round-1 frozen CAPIT configs + 4 round-2 regime-filter configs + 2 round-2 final portfolio configs
srs = []
for k, t in ALL.items():
    d = t[t.entry < DEV_END]; c = (d.gross - COSTS["bus_base"]).groupby(d.entry.dt.floor("D")).mean()
    if len(c) > 10 and c.std() > 0: srs.append(c.mean() / c.std())
SRVAR = float(np.var(srs)); out = {"n_total_variants": N_TOTAL, "round2_variants": len(fams["c1"]) + len(fams["c2"]) + len(fams["c4"]) + 4 + 2, "sr_var": SRVAR, "wf": {}}
print("cumulative variants", N_TOTAL, "round2", out["round2_variants"], "SRvar", round(SRVAR, 4))
for fam in ["c1_oi", "c1_capoi", "c1_pos", "c2", "c4"]:
    for cost in ["bus_base", "krk_maker"]:
        oos, picks = walk_forward(fams[fam], cost); s = wf_summary(oos, cost)
        if len(oos):
            dc = (oos.gross - COSTS[cost]).groupby(oos.entry.dt.floor("D")).mean()
            s["dsr"] = deflated_sharpe(dc.values, N_TOTAL, SRVAR)
            s["y2026"] = [int((oos.entry >= "2026-01-01").sum()), round(1e4 * (oos[oos.entry >= "2026-01-01"].gross - COSTS[cost]).mean(), 1)]
        s["picks"] = [p[1] for p in picks]; out["wf"][f"{fam}|{cost}"] = s
        print(f"{fam:9s} {cost:9s} n={s['n']:5d} mean={s['mean_bps']:7.1f} pf={s['pf']:.2f} folds={s['folds']} dsr={s.get('dsr', float('nan')):.3f} 2026={s.get('y2026')}")
json.dump(out, open("wf_r2.json", "w"), indent=1, default=str)

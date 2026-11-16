"""Comparator for oi_lookahead_fix.py: the same CAPIT mask with NO OI filter (holds 12/24/48), same harness.
Answers: how much does the OI-flush filter add on top of plain CAPIT on the same 12 coins?"""
import json
from r2common import *
from gates import permutation_test
P = panel(); K1 = P["k1h"]; W = json.load(open("wf_r2.json")); START = pd.Timestamp("2023-02-01", tz="UTC")
def capit(c):
    k = K1[c]; lv = np.log(k.c); sd = lv.diff().rolling(720, min_periods=360).std().shift(1)
    z = lv.diff(24) / (sd * np.sqrt(24)); vr = k.qv.rolling(24).sum() / (k.qv.rolling(720, min_periods=360).mean().shift(24) * 24)
    return (z <= -2.5) & (vr >= 2.0)
fam = {f"capit_only|h{h}": run_family(capit, K1, h)[0] for h in [12, 24, 48]}; out = {}
for cost in ["bus_base", "krk_maker"]:
    oos, _ = walk_forward(fam, cost); s = wf_summary(oos, cost)
    s["dsr"] = deflated_sharpe((oos.gross - COSTS[cost]).groupby(oos.entry.dt.floor("D")).mean().values, W["n_total_variants"], W["sr_var"])
    s["perm"] = permutation_test(oos, FOLDS[4].start_time.tz_localize("UTC"), DEV_END, cost)
    fz = fam["capit_only|h48"]; fz = fz[(fz.entry >= START) & (fz.entry < DEV_END)]; st = stats_of(fz.gross - COSTS[cost])
    fs = (fz.gross - COSTS[cost]).groupby(quarter(fz.entry)).sum().reindex(FOLDS).fillna(0)
    out[cost] = dict(wf=s, frozen_h48=dict(n=st["n"], pf=st["pf"], win=st["win"], mean_bps=st["mean_bps"], folds=f"{int((fs > 0).sum())}/15"))
    print(cost, "WF n", s["n"], "pf %.2f win %.3f" % (s["pf"], s["win"]), s["folds"], "dsr %.3f" % s["dsr"], "p", s["perm"]["p_value"], "| frozen h48", out[cost]["frozen_h48"], flush=True)
json.dump(out, open("oi_lookahead_capit_only.json", "w"), indent=1, default=str)

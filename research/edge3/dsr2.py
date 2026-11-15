"""Deflated Sharpe v2: every variant is converted to the SAME calendar-day series over its window (net return of
trades entered that day averaged, 0 on days without trades), so all trials share T and the cross-trial SR variance
is not inflated by tiny-sample variants. N = cumulative variants (both rounds)."""
import json
from r2common import *
DAYS = pd.date_range(pd.Timestamp("2023-02-01", tz="UTC"), DEV_END - pd.Timedelta("1D"), freq="D")
def daily(t, cost="bus_base", days=DAYS):
    d = t[(t.entry >= days[0]) & (t.entry < days[-1] + pd.Timedelta("1D"))]
    return (d.gross - COSTS[cost]).groupby(d.entry.dt.floor("D")).mean().reindex(days).fillna(0.0)
def sr(x): return x.mean() / x.std() if x.std() > 0 else 0.0
def all_trial_srvar():
    regs = {}
    for f in ["reg_idea1", "reg_idea2", "reg_idea3", "reg_idea4", "reg_idea5", "reg_c1", "reg_c2", "reg_c4"]:
        regs.update(pickle.load(open(f"/workspace/edge2-data/{f}.pkl", "rb")))
    s = [sr(daily(t)) for t in regs.values()]
    return float(np.var(s)), len(regs)
if __name__ == "__main__":
    v, n = all_trial_srvar(); json.dump({"srvar_daily": v, "n_regs": n}, open("dsr2.json", "w")); print(v, n)

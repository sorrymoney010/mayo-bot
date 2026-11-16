"""Edge hunt follow-up (declared after the WF run, before looking at it): FROZEN equal-weight ensemble of the six
A-family signals (SMA 50/100/200, TSMOM 7/14/28) on BTC and ETH, 50/50, no selection at all. This is the rule a paper
test would run. 1 extra trial (N=548)."""
import json, math
import numpy as np, pandas as pd
import hunt as Hm
fam = Hm.famA()
W = sum(fam.values()) / len(fam)                       # each coin weight in {0, 1/12, ..., 0.5}
mask = Hm.C.index >= pd.Timestamp("2019-01-01", tz="UTC")
out = {}
allsr = [Hm.sharpe(Hm.run_weights(w, Hm.COSTS["bus10"])[mask]) / math.sqrt(365) for w in list(fam.values()) + [W]]
for cn, cost in Hm.COSTS.items():
    r = Hm.run_weights(W, cost)[mask]; b = Hm.run_weights(Hm.bh(Hm.A_COLS), cost)[mask]
    yr, byr = Hm.yearly(r), Hm.yearly(b)
    n, pf, win = Hm.trades_pf(W.loc[mask], cost, "BTC"); n2, pf2, win2 = Hm.trades_pf(W.loc[mask], cost, "ETH")
    out[cn] = dict(stats=Hm.stats(r), bench=Hm.stats(b), by_year=yr, bench_by_year=byr,
                   folds_pos=f"{sum(v > 0 for v in yr.values())}/{len(yr)}", beats_bh_years=f"{sum(yr[y] > byr[y] for y in yr)}/{len(yr)}",
                   perm_p=Hm.perm_p((W.reindex(columns=Hm.C.columns).fillna(0.0), mask), cost, n=1000) if cn != "krk160" else None,
                   dsr=round(Hm.dsr(r.values, Hm.PRIOR_N + 19, float(np.var(allsr))), 3),
                   trades_pf_win_btc=[n, pf, win], trades_pf_win_eth=[n2, pf2, win2],
                   exposure=round(float(W.shift(1).loc[mask].sum(axis=1).mean()), 2),
                   since_2024=Hm.stats(r[r.index >= "2024-01-01"]), bh_since_2024=Hm.stats(b[b.index >= "2024-01-01"]))
    print(cn, json.dumps(out[cn]), flush=True)
json.dump(out, open("ensemble.json", "w"), indent=1)

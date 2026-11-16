"""Robustness for edge5 rules: half-year net@10bps, drop-best-5-days, date-clustered t-stat,
and joint-date permutation (same random bars for all coins, preserves cross-coin clustering)."""
import json, numpy as np, pandas as pd, lowvol_leadlag as m
RNG = np.random.default_rng(7)
dfs = {s: m.load(s) for s in m.ALTS + ["BTC", "ETH"]}
idx = dfs["BTC"].index
for s in dfs: dfs[s] = dfs[s].reindex(idx).ffill()
W = m.W
def sigA(lb, cond):
    out = {}
    for c in m.ALTS:
        d = dfs[c]; r = d["c"]/d["c"].shift(lb)-1; v = d["qv"].rolling(lb).sum()
        k = W*(3 if lb == 24 else 1)
        q10 = r.rolling(k).quantile(.10).shift(1); vmed = v.rolling(k).median().shift(1)
        out[c] = ((r <= q10) & ((v < vmed) if cond == "lowvol" else (v >= vmed))).fillna(False)
    return out
rl = dfs["BTC"]["c"]/dfs["BTC"]["o"]-1; sd = rl.rolling(W).std().shift(1); up = rl > 2*sd
def sigB(lag):
    return {c: ((up & ((dfs[c]["c"]/dfs[c]["o"]-1) < .5*rl)) if lag else up).fillna(False) for c in m.ALTS}
rules = {"A_24h_lowvol_h24": (sigA(24, "lowvol"), 24), "A_24h_highvol_h24": (sigA(24, "highvol"), 24),
         "A_1h_lowvol_h4": (sigA(1, "lowvol"), 4), "B_BTC_follow_h4": (sigB(False), 4), "B_BTC_lagged_h4": (sigB(True), 4)}
# joint-date returns matrix for permutation
fwd = {}
res = {}
for name, (sigs, H) in rules.items():
    tr = pd.concat([m.trades_from_signal(dfs[c], sigs[c], H).assign(coin=c) for c in m.ALTS])
    net = tr.r - .001
    day = tr.t.dt.floor("D")
    daily = net.groupby(day).sum()
    best5 = daily.nlargest(5).index
    keep = ~day.isin(best5)
    clus = net.groupby(tr.t).mean()  # cluster by entry hour
    tstat = clus.mean()/clus.std(ddof=1)*np.sqrt(len(clus))
    # joint-date permutation: draw len(clus) random hours, average alts' H-bar fwd return across coins
    o = np.vstack([dfs[c]["o"].values for c in m.ALTS])
    F = o[:, 1+H:]/o[:, 1:o.shape[1]-H]-1; F = F.mean(0)
    valid = np.arange(W+24, F.shape[0]); gclus = (tr.r).groupby(tr.t).mean().mean()
    null = np.array([F[RNG.choice(valid, len(clus), replace=False)].mean() for _ in range(2000)])
    hy = tr.assign(n=net, h=tr.t.dt.year.astype(str)+"H"+((tr.t.dt.month > 6)+1).astype(str)).groupby("h").n.agg(["count", "mean"])
    res[name] = {"n": int(len(tr)), "independent_entry_hours": int(len(clus)),
                 "net10_bps": round(1e4*net.mean(), 1),
                 "net10_bps_drop_best5days": round(1e4*net[keep].mean(), 1),
                 "net10_pf_drop_best5days": round(net[keep][net[keep] > 0].sum()/-net[keep][net[keep] < 0].sum(), 2),
                 "clustered_t_net10": round(float(tstat), 2),
                 "joint_date_perm_p_gross": round(float(((null >= gclus).sum()+1)/2001), 4),
                 "half_years_net10_bps": {k: [int(v["count"]), round(1e4*v["mean"], 1)] for k, v in hy.iterrows()}}
    print(name, json.dumps(res[name]))
json.dump(res, open("robustness.json", "w"), indent=1)

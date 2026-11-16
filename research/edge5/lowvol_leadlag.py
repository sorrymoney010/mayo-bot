"""Angle A (low-volume short-term reversal) and Angle B (BTC/ETH -> alt lead-lag).
Research only. Pre-specified rules, no parameter search. Binance spot 1h klines
(data.binance.vision), 2022-01-01 .. 2026-11-15 UTC.

No look-ahead: every threshold (return decile, volume median, sigma) uses bars
strictly before the signal bar (rolling(...).shift(1)). Signal on the CLOSE of
bar t; entry at OPEN of bar t+1; exit at OPEN of bar t+1+H. One position per
coin at a time (signals during an open position are skipped). Long only (spot);
mirrored short gross is reported for information only.
"""
import json, sys
import numpy as np, pandas as pd

D = "/workspace/edge2-data/pump_study/vision_%s_1h.pkl"
ETH = "/workspace/edge5/vision_ETHUSDT_1h.pkl"
ALTS = ["VET", "HBAR", "ALGO", "XLM"]
COST = {"binanceus_10bps": 0.0010, "kraken_limit_mkt_125bps": 0.0125, "kraken_taker_160bps": 0.0160}
RNG = np.random.default_rng(20261116)
W = 720  # 30 days of hourly bars for rolling thresholds

def load(sym):
    p = ETH if sym == "ETH" else D % (sym + "USDT")
    d = pd.read_pickle(p)
    d = d.asfreq("1h").ffill()   # 1 missing bar in 4.9y; ffill keeps index aligned
    return d

def trades_from_signal(df, sig, H):
    """sig: bool Series indexed like df (signal on close of bar t)."""
    o = df["o"].values; s = sig.values; n = len(o); out = []
    t = 0
    idx = np.flatnonzero(s)
    busy_until = -1
    for t in idx:
        e = t + 1; x = t + 1 + H
        if x >= n or t <= busy_until: continue
        out.append((df.index[e], o[x] / o[e] - 1.0))
        busy_until = x - 1
    return pd.DataFrame(out, columns=["t", "r"])

def stats(tr, cost):
    if len(tr) == 0: return {"n": 0}
    r = tr["r"].values - cost
    g, l = r[r > 0].sum(), -r[r < 0].sum()
    return {"n": int(len(r)), "mean_bps": round(1e4 * r.mean(), 1),
            "pf": round(g / l, 2) if l > 0 else None, "win": round(float((r > 0).mean()), 3)}

def folds(tr, cost):
    if len(tr) == 0: return []
    t = tr.assign(h=tr.t.dt.year.astype(str) + "H" + ((tr.t.dt.month > 6) + 1).astype(str))
    res = []
    for h, g in t.groupby("h"):
        res.append((h, int(len(g)), round(1e4 * (g.r.values - cost).mean(), 1)))
    return res

def perm_p(df_by_coin, sig_by_coin, H, obs_mean, n_iter=1000):
    """Null: same number of entries per coin at random bars, same hold."""
    pools = {}
    for c, df in df_by_coin.items():
        o = df["o"].values
        r = np.full(len(o), np.nan)
        r[:len(o) - H - 1] = o[1 + H:] / o[1:len(o) - H] - 1.0
        valid = np.flatnonzero(~np.isnan(r)); valid = valid[valid > W + 24]
        pools[c] = (r, valid, int(sig_by_coin[c]))
    tot = sum(v[2] for v in pools.values())
    if tot == 0: return None
    cnt = 0
    for _ in range(n_iter):
        s = 0.0
        for r, valid, k in pools.values():
            if k: s += r[RNG.choice(valid, k, replace=False)].sum()
        if s / tot >= obs_mean: cnt += 1
    return round((cnt + 1) / (n_iter + 1), 4)

def run_rule(name, dfs, sigs, H, desc):
    trs = {c: trades_from_signal(dfs[c], sigs[c], H) for c in sigs}
    allt = pd.concat([t.assign(coin=c) for c, t in trs.items()], ignore_index=True).sort_values("t")
    gross = allt.r.mean() if len(allt) else np.nan
    res = {"rule": name, "desc": desc, "hold_h": H,
           "gross_long_bps": round(1e4 * gross, 1) if len(allt) else None,
           "short_mirror_gross_bps": round(-1e4 * gross, 1) if len(allt) else None,
           "by_cost": {k: stats(allt, v) for k, v in COST.items()},
           "per_coin_gross_bps": {c: (int(len(t)), round(1e4 * t.r.mean(), 1) if len(t) else None) for c, t in trs.items()},
           "half_years_gross_bps": folds(allt, 0.0)}
    hy = res["half_years_gross_bps"]
    res["half_years_positive_gross"] = f"{sum(1 for h in hy if h[2] > 0)}/{len(hy)}"
    res["half_years_positive_net10"] = f"{sum(1 for h in hy if h[2] - 10 > 0)}/{len(hy)}"
    res["perm_p_gross"] = perm_p({c: dfs[c] for c in trs}, {c: len(t) for c, t in trs.items()}, H, gross) if len(allt) else None
    # baseline: unconditional mean H-bar return (drift) for comparison
    base = []
    for c in trs:
        o = dfs[c]["o"].values; base.append(np.nanmean(o[1 + H:] / o[1:len(o) - H] - 1.0))
    res["unconditional_drift_bps"] = round(1e4 * float(np.mean(base)), 1)
    return res

def main():
    dfs = {s: load(s) for s in ALTS + ["BTC", "ETH"]}
    idx = dfs["BTC"].index
    for s in dfs: dfs[s] = dfs[s].reindex(idx).ffill()
    out = {"data": f"Binance spot 1h, {idx[0]} .. {idx[-1]}", "costs_round_trip": COST, "rules": []}

    # ---------------- Angle A: volume-conditioned reversal ----------------
    for lb, H in [(1, 4), (24, 24)]:
        for cond in ["lowvol", "highvol"]:
            sigs = {}
            for c in ALTS:
                d = dfs[c]
                r = d["c"] / d["c"].shift(lb) - 1.0
                v = d["qv"].rolling(lb).sum()
                q10 = r.rolling(W * (3 if lb == 24 else 1)).quantile(0.10).shift(1)
                vmed = v.rolling(W * (3 if lb == 24 else 1)).median().shift(1)
                low = r <= q10
                sigs[c] = (low & (v < vmed)) if cond == "lowvol" else (low & (v >= vmed))
                sigs[c] = sigs[c].fillna(False)
            name = f"A_{lb}h_bottomdecile_{cond}_hold{H}"
            desc = (f"{lb}h return <= trailing 10th pct ({'30d' if lb==1 else '90d'} window) and "
                    f"{lb}h quote volume {'<' if cond=='lowvol' else '>='} trailing median; buy next open, hold {H}h")
            out["rules"].append(run_rule(name, dfs, sigs, H, desc))

    # ---------------- Angle B: lead-lag ----------------
    lagcorr = {}
    for L in ["BTC", "ETH"]:
        rl = np.log(dfs[L]["c"]).diff()
        for c in ALTS:
            ra = np.log(dfs[c]["c"]).diff()
            lagcorr[f"{L}->{c}"] = {"same_hour": round(float(rl.corr(ra)), 3),
                                    "leader_t_vs_alt_t+1": round(float(rl.corr(ra.shift(-1))), 4),
                                    "alt_t_vs_leader_t+1": round(float(ra.corr(rl.shift(-1))), 4)}
    out["lag_correlations_1h"] = lagcorr

    for L in ["BTC", "ETH"]:
        rl = dfs[L]["c"] / dfs[L]["o"] - 1.0
        sd = rl.rolling(W).std().shift(1)
        up = (rl > 2 * sd).fillna(False)
        for H in [1, 4]:
            sigs = {c: up for c in ALTS}
            out["rules"].append(run_rule(f"B_{L}_up2sigma_follow_hold{H}", dfs, sigs, H,
                f"{L} 1h bar return > 2x trailing 30d sigma; buy alt at next bar open, hold {H}h"))
    # laggard catch-up: BTC up >2 sigma in bar t, alt moved less than half of BTC's move in bar t
    rl = dfs["BTC"]["c"] / dfs["BTC"]["o"] - 1.0
    sd = rl.rolling(W).std().shift(1)
    up = (rl > 2 * sd)
    for H in [1, 4]:
        sigs = {}
        for c in ALTS:
            ra = dfs[c]["c"] / dfs[c]["o"] - 1.0
            sigs[c] = (up & (ra < 0.5 * rl)).fillna(False)
        out["rules"].append(run_rule(f"B_BTC_up2sigma_altLAGGED_hold{H}", dfs, sigs, H,
            f"BTC 1h > 2 sigma AND alt same-hour return < half of BTC's; buy alt next open, hold {H}h"))
    # down-side (information only, can't short on spot): BTC dump -> alt next bars
    dn = (rl < -2 * sd).fillna(False)
    out["rules"].append(run_rule("B_BTC_down2sigma_buyalt_hold4_INFO", dfs, {c: dn for c in ALTS}, 4,
        "BTC 1h < -2 sigma; long alt next open hold 4h (sign tells if dumps continue (neg) or bounce (pos))"))
    json.dump(out, open(sys.argv[1] if len(sys.argv) > 1 else "results.json", "w"), indent=1, default=str)
    for r in out["rules"]:
        b = r["by_cost"]
        print(f'{r["rule"]:42s} n={b["binanceus_10bps"]["n"]:4d} gross={r["gross_long_bps"]:7} drift={r["unconditional_drift_bps"]:6} '
              f'net10={b["binanceus_10bps"]["mean_bps"]:7} pf10={b["binanceus_10bps"]["pf"]} net125={b["kraken_limit_mkt_125bps"]["mean_bps"]:7} '
              f'pf125={b["kraken_limit_mkt_125bps"]["pf"]} HYpos={r["half_years_positive_gross"]} p={r["perm_p_gross"]}')
    print(json.dumps(lagcorr, indent=0))

if __name__ == "__main__":
    main()

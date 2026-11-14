"""Final candidate 'CAPIT-24' (post-capitulation bounce), pre-declared before the holdout:
 universe 12 coins; on each CLOSED 1h bar: z24 = 24h log return / (trailing-30d 1h sigma * sqrt(24)) <= -2.5
 AND 24h quote volume >= 2x trailing-30d average 24h volume -> buy next 1h open, sell 48h later.
 Portfolio: max 2 concurrent positions (1 per coin), 50% of equity each, ties -> most negative z.
Usage: python candidate.py dev | holdout"""
import sys, pickle, json, numpy as np, pandas as pd
from common import *
P = panel()
SPEC = dict(N=24, z=2.5, vm=2.0, hold=48, max_pos=2)
def signals(spec=SPEC, liq=False):
    rows = []
    for c in COINS:
        k = P["k1h"][c]; lv = np.log(k.c); sd = lv.diff().rolling(720, min_periods=360).std().shift(1)
        zz = lv.diff(spec["N"]) / (sd * np.sqrt(spec["N"]))
        volr = k.qv.rolling(spec["N"]).sum() / (k.qv.rolling(720, min_periods=360).mean().shift(spec["N"]) * spec["N"])
        sig = (zz <= -spec["z"]) & (volr >= spec["vm"])
        if liq:
            from features import rq
            pm = P["prem"][c].reindex(k.index).rolling(24, min_periods=12).mean(); sig &= pm <= rq(pm, 0.10)
        for t in k.index[sig.fillna(False).values]: rows.append((t, c, float(zz.loc[t])))
    return pd.DataFrame(rows, columns=["t", "coin", "z"]).sort_values(["t", "z"])
def portfolio(sg, start, end, spec=SPEC):
    """Sequential sim. Signal at bar t (closed) -> entry open t+1h, exit open t+1h+hold."""
    open_pos = []; trades = []
    for t, g in sg[(sg.t >= start) & (sg.t < end)].groupby("t"):
        open_pos = [p for p in open_pos if p["exit"] > t]
        for _, r in g.iterrows():
            if len(open_pos) >= spec["max_pos"]: break
            if any(p["coin"] == r.coin for p in open_pos): continue
            k = P["k1h"][r.coin]; e = t + pd.Timedelta("1h"); x = e + pd.Timedelta(hours=spec["hold"])
            if x not in k.index or e not in k.index: continue
            p = dict(coin=r.coin, entry=e, exit=x, z=r.z, px=k.o.loc[e], xp=k.o.loc[x]); p["gross"] = p["xp"] / p["px"] - 1
            # path stats for risk reporting
            seg = k.loc[e:x - pd.Timedelta("1h")]; p["mae"] = seg.l.min() / p["px"] - 1
            open_pos.append(p); trades.append(p)
    return pd.DataFrame(trades)
def report(t, label):
    out = {"label": label, "n": len(t)}
    if not len(t): return out
    q = quarter(t.entry)
    for cn, c in COSTS.items():
        net = t.gross - c; s = stats_of(net); fs = net.groupby(q).sum()
        out[cn] = dict(pf=round(s["pf"], 3), mean_bps=round(s["mean_bps"], 1), win=round(s["win"], 3), folds_traded_pos=f"{int((fs > 0).sum())}/{len(fs)}",
                       port_return_pct=round(100 * (0.5 * net).sum(), 1))  # each slot = 50% equity, non-compounded
        eq = np.cumsum(0.5 * net.values); out[cn]["max_dd_pct"] = round(100 * float((np.maximum.accumulate(np.r_[0, eq]) - np.r_[0, eq]).max()), 1)
    out["worst_trade_pct"] = round(100 * t.gross.min(), 1); out["worst_mae_pct"] = round(100 * t.mae.min(), 1)
    out["by_year"] = {int(y): [int(len(g)), round(1e4 * (g.gross - COSTS["bus_base"]).mean(), 1)] for y, g in t.groupby(t.entry.dt.year)}
    out["by_coin"] = {c: [int(len(g)), round(1e4 * (g.gross - COSTS["bus_base"]).mean(), 1)] for c, g in t.groupby("coin")}
    return out
if __name__ == "__main__":
    mode = sys.argv[1]
    START = pd.Timestamp("2023-02-01", tz="UTC")
    for liq in [False, True]:
        sg = signals(liq=liq); name = "CAPIT-24" + ("+liq" if liq else "")
        if mode == "dev":
            t = portfolio(sg, START, DEV_END); r = report(t, name + " dev")
            fs = (t.gross - COSTS["bus_base"]).groupby(quarter(t.entry)).sum().reindex(FOLDS).fillna(0)
            r["fold_pos_all15"] = f"{int((fs > 0).sum())}/15 (empty quarters count as not positive)"
            r["fold_sums_bps"] = {str(k): round(1e4 * v) for k, v in fs.items()}
            dc = (t.gross - COSTS["bus_base"]).groupby(t.entry.dt.floor("D")).mean()
            sv = json.load(open("wf_all.json"))["sr_var_dayclustered"]
            r["dsr_dayclustered_N376"] = round(deflated_sharpe(dc.values, 376, sv), 3); r["clusters"] = len(dc)
            net = (t.gross - COSTS["bus_base"]).groupby(t.entry.dt.floor("D")).sum().sort_values(ascending=False)
            r["net_sum_bps_all/ex_top3/ex_top5_days"] = [round(1e4 * net.sum()), round(1e4 * net.iloc[3:].sum()), round(1e4 * net.iloc[5:].sum())]
            t.to_csv(f"trades_{name}_dev.csv", index=False)
        else:
            t = portfolio(sg, DEV_END, HOLD_END); r = report(t, name + " HOLDOUT")
            t.to_csv(f"trades_{name}_holdout.csv", index=False)
            r["trades"] = t[["coin", "entry", "exit", "z", "gross", "mae"]].astype(str).values.tolist()
        print(json.dumps(r, indent=1, default=str)); json.dump(r, open(f"cand_{name}_{mode}.json", "w"), indent=1, default=str)

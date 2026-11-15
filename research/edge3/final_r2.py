"""Round-2 final candidates, frozen BEFORE the holdout, all gates applied (incl. CAPIT-24 baseline):
  A  OI-FLUSH-24 : 24h change in Binance USDT-M OI value <= its trailing-90d 2% quantile AND 24h spot price change < 0
                   -> buy next 1h open, sell 24h later. Max 2 concurrent, 50% slots, ties -> most negative OI change.
  B  CAPIT-24|R2 : frozen CAPIT-24, only when BTC's regime label (gates.py) is not 'bear'.
  C  CAPIT-24    : round-1 rule, re-gated.
Gates: >=30 trades, majority of 15 quarters positive, PF>=1.10 after fees, permutation p<0.01 (1,000 random-entry runs),
beats equal-weight B&H of the 12 coins over the same windows after fees, regime split, deflated Sharpe (daily, N cumulative),
non-negative holdout."""
import json
from r2common import *
from gates import *
import candidate as C
from dsr2 import daily, sr
P = panel(); M = pickle.load(open("/workspace/edge2-data/metrics_1h.pkl", "rb"))
SRVAR = json.load(open("dsr2.json"))["srvar_daily"]; NTOT = json.load(open("wf_r2.json"))["n_total_variants"]
START = pd.Timestamp("2023-02-01", tz="UTC")
def sig_oiflush():
    rows = []
    for c in COINS:
        k = P["k1h"][c]; d = M[c].oi.reindex(k.index).pct_change(24, fill_method=None)
        s = (d <= rq(d, 0.02)) & (k.c.pct_change(24) < 0)
        for t in k.index[s.fillna(False).values]: rows.append((t, c, float(d.loc[t])))
    return pd.DataFrame(rows, columns=["t", "coin", "z"]).sort_values(["t", "z"])
bd = None
def capit_R2():
    sg = C.signals(); ts = pd.DatetimeIndex(sg.t) + pd.Timedelta("1h"); return sg[regime_at("BTC", ts) != "bear"]
CANDS = {"A OI-FLUSH-24": (sig_oiflush, dict(C.SPEC, hold=24)), "B CAPIT-24|R2": (capit_R2, C.SPEC), "C CAPIT-24": (lambda: C.signals(), C.SPEC)}
out = {}
for name, (fn, spec) in CANDS.items():
    sg = fn(); td = C.portfolio(sg, START, DEV_END, spec); th = C.portfolio(sg, DEV_END, HOLD_END, spec)
    r = C.report(td, name); r.pop("by_coin", None)
    fs = (td.gross - COSTS["bus_base"]).groupby(quarter(td.entry)).sum().reindex(FOLDS).fillna(0)
    fk = (td.gross - COSTS["krk_maker"]).groupby(quarter(td.entry)).sum().reindex(FOLDS).fillna(0)
    r["folds_pos_of15"] = {"bus_base": int((fs > 0).sum()), "krk_maker": int((fk > 0).sum())}
    r["perm"] = {cn: permutation_test(td, START, DEV_END, cn) for cn in ["bus_base", "krk_maker"]}
    r["buy_hold"] = {cn: bh_table(pd.concat([td, th]), cn) for cn in ["bus_base", "krk_maker"]}
    r["regime_split_dev"] = regime_split(td); r["regime_split_holdout"] = regime_split(th) if len(th) else {}
    dd = daily(td); r["dsr_daily"] = round(deflated_sharpe(dd.values, NTOT, SRVAR), 3); r["daily_sr_ann"] = round(sr(dd) * np.sqrt(365), 2)
    r["holdout"] = dict(n=len(th), **({cn: dict(pf=round(stats_of(th.gross - COSTS[cn])["pf"], 2), mean_bps=round(stats_of(th.gross - COSTS[cn])["mean_bps"], 1),
                                                 ret_pct=round(100 * (0.5 * (th.gross - COSTS[cn])).sum(), 1)) for cn in COSTS} if len(th) else {}),
                        trades=th[["coin", "entry", "gross"]].astype(str).values.tolist() if len(th) else [])
    b = r["buy_hold"]["bus_base"]
    r["GATES"] = dict(trades30=r["n"] >= 30, folds_majority=r["folds_pos_of15"]["bus_base"] > 7, pf110_bus=r["bus_base"]["pf"] >= 1.10,
                      pf110_krk_maker=r["krk_maker"]["pf"] >= 1.10, perm_p01=r["perm"]["bus_base"]["p_value"] < 0.01,
                      beats_bh_dev=b["dev"]["beats"], beats_bh_every_year=all(b[y]["beats"] for y in ["2023", "2024", "2025", "2026"]),
                      holdout_nonneg=(len(th) > 0 and (th.gross - COSTS["bus_base"]).mean() >= 0), dsr_ge_0_95=r["dsr_daily"] >= 0.95)
    out[name] = r; td.to_csv(f"trades_{name.split()[1]}_dev.csv", index=False); th.to_csv(f"trades_{name.split()[1]}_holdout.csv", index=False)
    print("=" * 20, name); print(json.dumps({k: v for k, v in r.items() if k not in ("by_coin",)}, indent=0, default=str)[:6000], flush=True)
json.dump(out, open("final_r2.json", "w"), indent=1, default=str)

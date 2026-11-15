"""Round 2 / idea 3: CAPIT-24 + the extra gates, and two PRE-SPECIFIED regime filters (no tuning):
  R1: BTC previous daily close > BTC 200-day SMA.
  R2: BTC regime label (gates.py: EMA200 slope + ADX<20 flat, 3-bar persistence) is not 'bear'.
Stablecoin-dominance trend was considered but has no free reliable hourly/daily history reachable from here -> skipped."""
import json, glob
from r2common import *
from gates import *
from gates import _daily
import candidate as C          # research/edge2/candidate.py (frozen rule)
P = panel()
bd = _daily("BTC"); sma = bd.c.rolling(200).mean()
r1 = (bd.c > sma); r1.index = r1.index + pd.Timedelta("1D")            # known at that day's close
def filt(sg, kind):
    if kind == "none": return sg
    ts = pd.DatetimeIndex(sg.t) + pd.Timedelta("1h")                    # decision time = bar close
    if kind == "R1": ok = r1.reindex(ts, method="ffill").fillna(False).values
    else: ok = regime_at("BTC", ts) != "bear"
    return sg[ok]
START = pd.Timestamp("2023-02-01", tz="UTC")
out = {}
for liq in [False, True]:
    sg0 = C.signals(liq=liq)
    for kind in ["none", "R1", "R2"]:
        sg = filt(sg0, kind); name = f"CAPIT-24{'+liq' if liq else ''}|{kind}"
        td = C.portfolio(sg, START, DEV_END); th = C.portfolio(sg, DEV_END, HOLD_END)
        r = C.report(td, name); r.pop("by_coin", None)
        fs = (td.gross - COSTS["bus_base"]).groupby(quarter(td.entry)).sum().reindex(FOLDS).fillna(0)
        r["folds_pos_of15"] = int((fs > 0).sum())
        r["perm_dev_bus"] = permutation_test(td, START, DEV_END, "bus_base")
        r["perm_dev_krk"] = permutation_test(td, START, DEV_END, "krk_maker")
        r["buy_hold_bus"] = bh_table(pd.concat([td, th]), "bus_base")
        r["regime_split_dev"] = regime_split(td)
        r["holdout"] = dict(n=len(th), mean_bus_bps=round(1e4 * (th.gross - COSTS["bus_base"]).mean(), 1) if len(th) else None)
        out[name] = r
        print(name, "n", r["n"], "PF bus", r["bus_base"]["pf"], "krk", r["krk_maker"]["pf"], "folds", r["folds_pos_of15"], "by_year", r["by_year"],
              "perm p", r["perm_dev_bus"]["p_value"], "| regimes", r["regime_split_dev"], "| B&H", {k: (v["strat_pct"], v["bh_pct"]) for k, v in r["buy_hold_bus"].items()}, "| hold", r["holdout"], flush=True)
json.dump(out, open("out_c3.json", "w"), indent=1, default=str)

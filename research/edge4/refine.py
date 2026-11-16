"""Edge4 (2026-11-16): pre-specified refinements of CAPIT-24|R2 (BTC-not-bear) for a $100 Kraken spot book.
Every variant below was written down BEFORE running; nothing was tuned after seeing results.
  V0  CAPIT-24 (no regime)                  reference, already tested (edge2)
  V1  CAPIT-24|R2                           reference, already tested (edge3/c3)
  V2  R2, hold 24h                          shorter time stop
  V3  R2, hold 12h                          intraday time stop
  V4  R2, catastrophe stop -15% (fill at stop -50 bps slip), hold 48h
  V5  R2, LIMIT entry: buy limit = signal-bar close x 0.995, live for the next 1h bar only; skip if not touched.
      Fee model: maker entry (40) + taker exit (80+5 slip) = 125 bps RT on Kraken tier 1 ("krk_mix").
  V6  R2, skip if funding is in its trailing-90d bottom 10% (Shek 2026: down-day + extreme negative funding continues)
  V7  R2, require funding in trailing-90d bottom 10% (the opposite, liquidation-flush reading)
  V8  R2, 15m confirmation: after the 1h signal, enter at the open of the 15m bar after the FIRST 15m bar that
      closes green (close>open), within 4h of the signal; else skip.
Costs (round trip): bus_base 10, krk_maker 80 (maker both sides, best case), krk_mix 125 (taker entry or exit), krk_taker 170.
Portfolio: max 2 concurrent positions, 1 per coin, ties -> most negative z (same as frozen CAPIT-24).
Dev 2023-02-01..2026-09-30, holdout 2026-10-01..2026-11-13. DSR: day-clustered, N = 520 prior + 7 new = 527."""
import sys, json
sys.path.insert(0, "/workspace/mayo-bot/research/edge3"); sys.path.insert(0, "/workspace/mayo-bot/research/edge2")
from r2common import *
from gates import permutation_test, regime_at, bh_table, regime_split
from features import funding_on_bars, rq
import candidate as C
COSTS["krk_mix"] = (40 + 85) / 1e4
P = panel(); K1, K15 = P["k1h"], P["k15"]
START = pd.Timestamp("2023-02-01", tz="UTC"); W = json.load(open("/workspace/mayo-bot/research/edge3/wf_r2.json"))
N_TRIALS = W["n_total_variants"] + 7
H = pd.Timedelta("1h")
FUND = {}
for c in COINS:
    f = funding_on_bars(P["fund"][c], K1[c].index); FUND[c] = (f, rq(f, 0.10))
def base_signals():
    sg = C.signals(); ts = pd.DatetimeIndex(sg.t) + H
    sg["r2"] = regime_at("BTC", ts) != "bear"; return sg
def fund_low(c, t):
    f, q = FUND[c]; return bool(f.loc[t] <= q.loc[t]) if (t in f.index and pd.notna(q.loc[t])) else False
def entry_fn(kind):
    def market(c, t):
        k = K1[c]; e = t + H
        return (e, k.o.loc[e]) if e in k.index else None
    def limit(c, t):
        k = K1[c]; e = t + H
        if e not in k.index: return None
        lim = k.c.loc[t] * 0.995
        return (e, min(lim, k.o.loc[e])) if k.l.loc[e] <= lim else None   # gap-down open below limit fills at open
    def conf15(c, t):
        k = K15[c]; w = k.loc[t + H: t + H + pd.Timedelta("4h") - pd.Timedelta("15min")]
        g = w[w.c > w.o]
        if not len(g): return None
        e = g.index[0] + pd.Timedelta("15min")
        return (e, k.o.loc[e]) if e in k.index else None
    return dict(market=market, limit=limit, conf15=conf15)[kind]
def portfolio(sg, start, end, hold=48, stop=None, entry="market", max_pos=2):
    ef = entry_fn(entry); open_pos, trades = [], []
    for t, g in sg[(sg.t >= start) & (sg.t < end)].groupby("t"):
        open_pos = [p for p in open_pos if p["exit"] > t]
        for _, r in g.iterrows():
            if len(open_pos) >= max_pos: break
            if any(p["coin"] == r.coin for p in open_pos): continue
            en = ef(r.coin, t)
            if en is None: continue
            e, px = en; k = K15[r.coin]; x = e + pd.Timedelta(hours=hold)
            if x not in k.index: continue
            seg = k.loc[e: x - pd.Timedelta("15min")]; xp, why, xt = k.o.loc[x], "time", x
            if stop is not None:
                hit = seg.index[seg.l.values <= px * (1 - stop)]
                if len(hit): xt = hit[0] + pd.Timedelta("15min"); xp = min(px * (1 - stop), seg.o.loc[hit[0]]) * (1 - 0.005); why = "stop"
            p = dict(coin=r.coin, entry=e, exit=xt, z=r.z, px=px, xp=xp, why=why, mae=seg.l.min() / px - 1)
            p["gross"] = xp / px - 1; open_pos.append(p); trades.append(p)
    return pd.DataFrame(trades)
def summarize(td, th, name):
    out = {"name": name, "n_dev": len(td)}
    q = quarter(td.entry)
    for cn in ["bus_base", "krk_maker", "krk_mix", "krk_taker"]:
        net = td.gross - COSTS[cn]; s = stats_of(net); fs = net.groupby(q).sum().reindex(FOLDS).fillna(0)
        eq = np.cumsum(0.5 * net.values); dd = float((np.maximum.accumulate(np.r_[0, eq]) - np.r_[0, eq]).max())
        out[cn] = dict(pf=round(s["pf"], 2), mean_bps=round(s["mean_bps"], 1), win=round(s["win"], 3), q_pos=f"{int((fs > 0).sum())}/15",
                       ret_pct_50slots=round(100 * (0.5 * net).sum(), 1), max_dd_pct=round(100 * dd, 1))
    out["by_year_krk_mix_bps"] = {int(y): [len(g), round(1e4 * (g.gross - COSTS["krk_mix"]).mean(), 1)] for y, g in td.groupby(td.entry.dt.year)}
    dc = (td.gross - COSTS["krk_mix"]).groupby(td.entry.dt.floor("D")).mean()
    out["dsr_krk_mix"] = round(deflated_sharpe(dc.values, N_TRIALS, W["sr_var"]), 3)
    dcb = (td.gross - COSTS["bus_base"]).groupby(td.entry.dt.floor("D")).mean()
    out["dsr_bus"] = round(deflated_sharpe(dcb.values, N_TRIALS, W["sr_var"]), 3)
    out["perm_p_krk_mix"] = permutation_test(td, START, DEV_END, "krk_mix", n_runs=1000)["p_value"]
    out["worst_trade_pct"] = round(100 * td.gross.min(), 1); out["worst_mae_pct"] = round(100 * td.mae.min(), 1)
    out["stops"] = int((td.why == "stop").sum())
    net_days = (td.gross - COSTS["krk_mix"]).groupby(td.entry.dt.floor("D")).sum().sort_values(ascending=False)
    out["net_sum_bps_all_ex3_ex5_days"] = [round(1e4 * net_days.sum()), round(1e4 * net_days.iloc[3:].sum()), round(1e4 * net_days.iloc[5:].sum())]
    out["bh_krk_mix"] = {k: (v["strat_pct"], v["bh_pct"]) for k, v in bh_table(pd.concat([td, th]), "krk_mix").items()}
    out["regime_split"] = {k: v for k, v in regime_split(td, ("krk_maker",)).items()}
    out["holdout"] = dict(n=len(th), mean_krk_mix_bps=round(1e4 * (th.gross - COSTS["krk_mix"]).mean(), 1) if len(th) else None,
                          trades=th[["coin", "entry", "gross"]].astype(str).values.tolist() if len(th) else [])
    return out
if __name__ == "__main__":
    sg = base_signals(); r2 = sg[sg.r2]
    sg["flow"] = [fund_low(c, t) for c, t in zip(sg.coin, sg.t)]; r2 = sg[sg.r2]
    V = {"V0_capit": (sg, {}), "V1_r2": (r2, {}), "V2_r2_h24": (r2, dict(hold=24)), "V3_r2_h12": (r2, dict(hold=12)),
         "V4_r2_stop15": (r2, dict(stop=0.15)), "V5_r2_limit": (r2, dict(entry="limit")),
         "V6_r2_skipfundlow": (r2[~r2.flow], {}), "V7_r2_needfundlow": (r2[r2.flow], {}), "V8_r2_conf15": (r2, dict(entry="conf15"))}
    res = {}
    for name, (s, kw) in V.items():
        td = portfolio(s, START, DEV_END, **kw); th = portfolio(s, DEV_END, HOLD_END, **kw)
        res[name] = summarize(td, th, name); r = res[name]
        print(name, "n", r["n_dev"], "| PF bus", r["bus_base"]["pf"], r["bus_base"]["q_pos"], "| krk80", r["krk_maker"]["pf"], r["krk_maker"]["q_pos"],
              "| krk125", r["krk_mix"]["pf"], r["krk_mix"]["mean_bps"], r["krk_mix"]["q_pos"], "dd", r["krk_mix"]["max_dd_pct"], "| krk170", r["krk_taker"]["pf"],
              "| DSR bus/k125", r["dsr_bus"], r["dsr_krk_mix"], "| p", r["perm_p_krk_mix"], "| yrs", r["by_year_krk_mix_bps"], "| hold", r["holdout"]["n"], r["holdout"]["mean_krk_mix_bps"], flush=True)
    json.dump(res, open("/workspace/mayo-bot/research/edge4/refine.json", "w"), indent=1, default=str)

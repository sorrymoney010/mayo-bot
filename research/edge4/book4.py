"""Edge4: the two finalists (V1 CAPIT-24|R2 market entry, V5 CAPIT-24|R2 limit entry) on the CURRENT paper book
VET/HBAR/ALGO/XLM, Binance Vision USDT 1h, 2022-01-01..2026-11-15 (data from the 2026-11-15 low-price study, copied to
/workspace/edge2-data/pump_study). Same rules, no new parameters. BTC regime R2 computed from Vision BTC daily."""
import sys, json
sys.path.insert(0, "/workspace/mayo-bot/research/edge3"); sys.path.insert(0, "/workspace/mayo-bot/research/edge2")
import numpy as np, pandas as pd, gates
from common import stats_of, deflated_sharpe
D = "/workspace/edge2-data/pump_study"; COINS4 = ["VET", "HBAR", "ALGO", "XLM"]
gates._daily = lambda c: pd.read_pickle(f"{D}/vision_{c}USDT_1d.pkl")[["o", "h", "l", "c"]].astype(float)
K = {c: pd.read_pickle(f"{D}/vision_{c}USDT_1h.pkl").astype(float) for c in COINS4}
H = pd.Timedelta("1h"); COST = dict(bus=10e-4, krk80=80e-4, krk125=125e-4, krk170=170e-4)
W = json.load(open("/workspace/mayo-bot/research/edge3/wf_r2.json")); N = W["n_total_variants"] + 7
def signals():
    rows = []
    for c, k in K.items():
        lv = np.log(k.c); sd = lv.diff().rolling(720, min_periods=360).std().shift(1); zz = lv.diff(24) / (sd * np.sqrt(24))
        vr = k.qv.rolling(24).sum() / (k.qv.rolling(720, min_periods=360).mean().shift(24) * 24)
        for t in k.index[((zz <= -2.5) & (vr >= 2.0)).fillna(False).values]: rows.append((t, c, float(zz.loc[t])))
    sg = pd.DataFrame(rows, columns=["t", "coin", "z"]).sort_values(["t", "z"])
    sg["r2"] = gates.regime_at("BTC", pd.DatetimeIndex(sg.t) + H) != "bear"; return sg
def book(sg, limit, hold=48):
    open_pos, tr = [], []
    for t, g in sg.groupby("t"):
        open_pos = [p for p in open_pos if p["exit"] > t]
        for _, r in g.iterrows():
            if len(open_pos) >= 2 or any(p["coin"] == r.coin for p in open_pos): continue
            k = K[r.coin]; e = t + H; x = e + pd.Timedelta(hours=hold)
            if e not in k.index or x not in k.index: continue
            if limit:
                lim = k.c.loc[t] * 0.995
                if k.l.loc[e] > lim: continue
                px = min(lim, k.o.loc[e])
            else: px = k.o.loc[e]
            p = dict(coin=r.coin, entry=e, exit=x, gross=k.o.loc[x] / px - 1); open_pos.append(p); tr.append(p)
    return pd.DataFrame(tr)
sg = signals(); sg = sg[sg.t >= pd.Timestamp("2022-08-01", tz="UTC")]     # BTC regime warmup (200d) ends ~Jul 2022
out = {}
for name, s, lim in [("4coin_noR2_market", sg, False), ("4coin_noR2_limit", sg, True), ("4coin_R2_market", sg[sg.r2], False), ("4coin_R2_limit", sg[sg.r2], True)]:
    t = book(s, lim); q = pd.PeriodIndex(t.entry.dt.tz_localize(None), freq="Q"); nq = len(pd.period_range("2022Q3", "2026Q4", freq="Q"))
    r = {"n": len(t)}
    for cn, c in COST.items():
        net = t.gross - c; st = stats_of(net); fs = net.groupby(q).sum()
        r[cn] = dict(pf=round(st["pf"], 2), mean_bps=round(st["mean_bps"], 1), q_pos=f"{int((fs > 0).sum())}/{nq}")
    dc = (t.gross - COST["krk125"]).groupby(t.entry.dt.floor("D")).mean(); r["dsr_krk125"] = round(deflated_sharpe(dc.values, N, W["sr_var"]), 3)
    r["by_year_krk125"] = {int(y): [len(g), round(1e4 * (g.gross - COST["krk125"]).mean(), 1)] for y, g in t.groupby(t.entry.dt.year)}
    nd = (t.gross - COST["krk125"]).groupby(t.entry.dt.floor("D")).sum().sort_values(ascending=False)
    r["net_bps_all_ex3_ex5_days"] = [round(1e4 * nd.sum()), round(1e4 * nd.iloc[3:].sum()), round(1e4 * nd.iloc[5:].sum())]
    out[name] = r; print(name, json.dumps(r), flush=True)
json.dump(out, open("/workspace/mayo-bot/research/edge4/book4.json", "w"), indent=1)

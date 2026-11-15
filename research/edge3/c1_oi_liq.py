"""Round 2 / idea 1: open interest & liquidation proxies (Binance USDT-M public 'metrics', 5m -> 1h).
 (a) long-liquidation proxy: N-hour OI-value change in its bottom-q tail (trailing 90d) while 24h price change < 0;
 (b) OI flush + CAPIT-24 event (z24<=-2.5 & 24h vol>=2x) with 24h OI change in bottom q;
 (c) positioning extremes, contrarian long: crowd L/S account ratio, top-trader L/S position ratio, taker L/S volume
     ratio in their bottom-q tail. Spot long only; time exit. (No free historical liquidation feed: Binance's
     liquidationSnapshot archive is not published for this period, so OI drops are the liquidation proxy.)"""
import itertools
from r2common import *
P = panel(); K1 = P["k1h"]; M = pickle.load(open("/workspace/edge2-data/metrics_1h.pkl", "rb"))
rows, reg = [], {}
def add(name, t, bm):
    r = evaluate(t, name, base_mean=bm); r["dc_t"], r["dc_days"] = day_clustered_t(t); rows.append(r); reg[name] = t
def capit(c):
    k = K1[c]; lv = np.log(k.c); sd = lv.diff().rolling(720, min_periods=360).std().shift(1)
    z = lv.diff(24) / (sd * np.sqrt(24)); vr = k.qv.rolling(24).sum() / (k.qv.rolling(720, min_periods=360).mean().shift(24) * 24)
    return (z <= -2.5) & (vr >= 2.0)
CAP = {c: capit(c) for c in COINS}
OI = {c: M[c].oi.reindex(K1[c].index) for c in COINS}
for N, q, hold in itertools.product([4, 12, 24], [0.02, 0.05], [12, 24, 48]):
    f = lambda c, N=N, q=q: (lambda d: (d <= rq(d, q)) & (K1[c].c.pct_change(24) < 0))(OI[c].pct_change(N, fill_method=None))
    t, bm = run_family(f, K1, hold); add(f"OI|drop{N}h|q{q}|pxdown|h{hold}", t, bm)
for q, hold in itertools.product([0.10, 0.25], [12, 24, 48]):
    f = lambda c, q=q: (lambda d: CAP[c] & (d <= rq(d, q)))(OI[c].pct_change(24, fill_method=None))
    t, bm = run_family(f, K1, hold); add(f"OI|capit+oiflush|q{q}|h{hold}", t, bm)
for feat, q, hold in itertools.product(["ls_crowd", "ls_top", "taker_ratio"], [0.02, 0.05], [12, 24, 48]):
    f = lambda c, feat=feat, q=q: (lambda x: x <= rq(x, q))(M[c][feat].reindex(K1[c].index))
    t, bm = run_family(f, K1, hold); add(f"POS|{feat}|low{q}|h{hold}", t, bm)
df = pd.DataFrame(rows); df.to_csv("out_c1.csv", index=False); pickle.dump(reg, open("/workspace/edge2-data/reg_c1.pkl", "wb")); show(df)

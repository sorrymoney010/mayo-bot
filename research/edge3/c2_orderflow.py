"""Round 2 / idea 2: order flow from spot klines (taker-buy quote volume).
 (a) taker imbalance extremes on 1h (W=1,4,24h) vs trailing-90d quantile, low tail (sell pressure, contrarian long)
     and high tail (buy pressure, momentum long); (b) the same on 15m bars (W=1,4 bars);
 (c) CVD divergence: close at a new L-hour low while L-hour CVD change is positive (absorption) -> long."""
import itertools
from r2common import *
P = panel(); rows, reg = [], {}
def add(name, t, bm):
    r = evaluate(t, name, base_mean=bm); r["dc_t"], r["dc_days"] = day_clustered_t(t); rows.append(r); reg[name] = t
K1, K15 = P["k1h"], P["k15"]
imb1 = {c: {W: (2 * K1[c].tbq.rolling(W).sum() - K1[c].qv.rolling(W).sum()) / K1[c].qv.rolling(W).sum() for W in [1, 4, 24]} for c in COINS}
for W, q, side, hold in itertools.product([1, 4, 24], [0.02, 0.05], ["low", "high"], [12, 24, 48]):
    f = lambda c: (imb1[c][W] <= rq(imb1[c][W], q)) if side == "low" else (imb1[c][W] >= rq(imb1[c][W], 1 - q))
    t, bm = run_family(f, K1, hold); add(f"OF|1h|W{W}|{side}{q}|h{hold}", t, bm)
imb15 = {c: {W: (2 * K15[c].tbq.rolling(W).sum() - K15[c].qv.rolling(W).sum()) / K15[c].qv.rolling(W).sum() for W in [1, 4]} for c in COINS}
for W, q, side, holdh in itertools.product([1, 4], [0.01, 0.02], ["low", "high"], [4, 12]):
    f = lambda c: (imb15[c][W] <= rq(imb15[c][W], q, win=8640, minp=2880)) if side == "low" else (imb15[c][W] >= rq(imb15[c][W], 1 - q, win=8640, minp=2880))
    t, bm = run_family(f, K15, holdh * 4); add(f"OF|15m|W{W}|{side}{q}|h{holdh}h", t, bm)
for L, hold, thr in itertools.product([12, 24, 48], [12, 24, 48], ["pos", "p80"]):
    def f(c, L=L, thr=thr):
        k = K1[c]; cvd = (2 * k.tbq - k.qv).cumsum(); dc = cvd.diff(L) / k.qv.rolling(L).sum()
        newlow = k.c <= k.c.rolling(L).min()
        cond = dc > 0 if thr == "pos" else dc >= rq(dc, 0.80)
        return newlow & cond
    t, bm = run_family(f, K1, hold); add(f"OF|cvddiv|L{L}|{thr}|h{hold}", t, bm)
df = pd.DataFrame(rows); df.to_csv("out_c2.csv", index=False); pickle.dump(reg, open("/workspace/edge2-data/reg_c2.pkl", "wb"))
show(df)

"""Round 2 / idea 4: volatility-compression breakout with volume confirmation (only if gross edge shows).
 compression: 1h ATR14/price in the bottom q of its trailing-90d distribution at some bar in the prior 24h;
 breakout: close > highest high of prior L bars and 1h quote volume >= vm x trailing-30d mean; time exit."""
import itertools
from r2common import *
P = panel(); K1 = P["k1h"]; rows, reg = [], {}
F = {}
for c in COINS:
    k = K1[c]; tr = np.maximum(k.h - k.l, np.maximum((k.h - k.c.shift()).abs(), (k.l - k.c.shift()).abs()))
    an = tr.rolling(14).mean() / k.c; F[c] = dict(an=an, vavg=k.qv.rolling(720, min_periods=360).mean().shift(1))
for q, L, vm, hold in itertools.product([0.05, 0.10], [24, 48], [1.0, 1.5], [12, 24, 48]):
    def f(c, q=q, L=L, vm=vm):
        k = K1[c]; an = F[c]["an"]; comp = (an <= rq(an, q)).rolling(24).max().astype(bool)
        return comp & (k.c > k.h.rolling(L).max().shift(1)) & (k.qv >= vm * F[c]["vavg"])
    t, bm = run_family(f, K1, hold); name = f"VC|q{q}|L{L}|vm{vm}|h{hold}"
    r = evaluate(t, name, base_mean=bm); r["dc_t"], _ = day_clustered_t(t); rows.append(r); reg[name] = t
df = pd.DataFrame(rows); df.to_csv("out_c4.csv", index=False); pickle.dump(reg, open("/workspace/edge2-data/reg_c4.pkl", "wb")); show(df)

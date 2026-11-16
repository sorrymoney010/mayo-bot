"""Extra luck test: iid (mean block 1) and 10-day stationary bootstrap; appends to verify.json."""
import json, numpy as np, pandas as pd, data, trend6 as t
S, E = "2019-01-01", "2026-10-31"; rng = np.random.default_rng(7)
px = data.load("coinbase"); W = t.target_weights(px)
real_ex = t.metrics(t.simulate(px, W, S, E, 10)["ret"])["sharpe"] - t.metrics(t.bench(px, S, E))["sharpe"]
P = pd.DataFrame({c: px[c]["open"] for c in t.COINS}).loc["2018-01-01":E]
lr = np.log(P / P.shift(1)).dropna().values; ix = pd.date_range("2018-01-01", E, freq="D")
out = json.load(open("verify.json"))
for mb in (1, 10):
    ex = []
    for _ in range(500):
        n = len(ix); path = np.empty((n, 2)); i = rng.integers(len(lr))
        for d in range(n):
            if rng.random() < 1 / mb: i = rng.integers(len(lr))
            path[d] = lr[i % len(lr)]; i += 1
        path = 100 * np.exp(np.cumsum(path, 0))
        sp = {c: pd.DataFrame({"open": path[:, j], "close": np.r_[path[1:, j], path[-1, j]]}, index=ix) for j, c in enumerate(t.COINS)}
        ex.append(t.metrics(t.simulate(sp, t.target_weights(sp), S, E, 10)["ret"])["sharpe"] - t.metrics(t.bench(sp, S, E))["sharpe"])
    ex = np.array(ex)
    out["luck_block_bootstrap"][f"mean_block_{mb}d"] = dict(p=float((ex >= real_ex).mean()), null_mean=float(ex.mean()), null_p95=float(np.percentile(ex, 95)), real_excess_sharpe=float(real_ex))
    print(mb, out["luck_block_bootstrap"][f"mean_block_{mb}d"])
json.dump(out, open("verify.json", "w"), indent=1, default=str)

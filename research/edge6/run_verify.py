"""TREND-6 independent verification driver. Writes research/edge6/verify.json and log_verify.txt."""
import json, sys
import numpy as np
import pandas as pd
import data, trend6 as t

S, E = "2019-01-01", "2026-10-31"
rng = np.random.default_rng(20261116)
OUT = {}
log = open("log_verify.txt", "w")
def p(*a):
    s = " ".join(str(x) for x in a); print(s); log.write(s + "\n")

def summary(ret, sim=None):
    m = t.metrics(ret)
    d = dict(cagr=round(m["cagr"] * 100, 1), sharpe=round(m["sharpe"], 2), maxdd=round(m["maxdd"] * 100, 1),
             since2024=round(m["since2024"] * 100, 1), yearly={k: round(v * 100, 1) for k, v in m["yearly"].items()},
             up_years=sum(v > 0 for v in m["yearly"].values()), n_years=len(m["yearly"]))
    if sim is not None:
        d.update({k: (round(v, 3) if isinstance(v, float) else v) for k, v in t.trades(sim).items()})
        d["exposure"] = round(float(sim["held"].sum(1).mean()), 3)
        d["turnover_x_per_yr"] = round(float(sim["turnover"].sum() / (len(ret) / 365.25)), 1)
    return d

px_all = {s: data.load(s) for s in ("coinbase", "vision", "binanceus")}

# 1. Headline replication on 3 data sources, 3 accounting policies
p("== 1. headline 2019-01-01..2026-10-31, 10 bps RT, next-open ==")
OUT["headline"] = {}
for src in ("coinbase", "vision"):
    px = px_all[src]; W = t.target_weights(px)
    for pol in ("target", "on_change", "fixed"):
        sim = t.simulate(px, W, S, E, 10, "next_open", pol)
        r = summary(sim["ret"], sim); OUT["headline"][f"{src}/{pol}"] = r; p(src, pol, r)
    for kind in ("rebal", "hold", "BTC", "ETH"):
        r = summary(t.bench(px, S, E, kind)); OUT["headline"][f"{src}/BH_{kind}"] = r; p(src, "BH", kind, r)
    expo = OUT["headline"][f"{src}/target"]["exposure"]
    r = summary(t.bench(px, S, E, "rebal", exposure=expo)); OUT["headline"][f"{src}/BH_rebal_x{expo}"] = r; p(src, "BH exposure-matched", r)
# Binance.US: only usable 2020-05.. 2023-06 and 2025-02.. ; report the post-relist window as a spot check
px = px_all["binanceus"]; W = t.target_weights(px)
sim = t.simulate(px, W, "2025-09-15", E, 10, "next_open", "target")
OUT["binanceus_spot_2025-09-15"] = summary(sim["ret"], sim)
for src in ("coinbase", "vision"):
    pxs = px_all[src]; Ws = t.target_weights(pxs)
    OUT[f"{src}_spot_2025-09-15"] = summary(t.simulate(pxs, Ws, "2025-09-15", E, 10)["ret"])
p("binance.us spot check 2025-09-15..", OUT["binanceus_spot_2025-09-15"]["cagr"], "coinbase", OUT["coinbase_spot_2025-09-15"]["cagr"], "vision", OUT["vision_spot_2025-09-15"]["cagr"])
# signal agreement between sources
Wc = t.target_weights(px_all["coinbase"]).loc[S:E]; Wv = t.target_weights(px_all["vision"]).loc[S:E]
OUT["signal_agreement_coinbase_vs_vision"] = {c: round(float((np.abs(Wc[c] - Wv[c]) < 1e-9).mean()), 4) for c in t.COINS}
p("signal agreement", OUT["signal_agreement_coinbase_vs_vision"])

# 2. Sensitivity: costs x execution mode (coinbase, target policy)
p("== 2. cost x execution ==")
px = px_all["coinbase"]; W = t.target_weights(px)
OUT["sens"] = {}
for mode in ("next_open", "close", "delay1", "lookahead_bug"):
    for bps in (0, 5, 10, 20, 80, 160):
        sim = t.simulate(px, W, S, E, bps, mode, "target")
        r = summary(sim["ret"], sim); OUT["sens"][f"{mode}/{bps}"] = r
        p(mode, bps, {k: r[k] for k in ("cagr", "sharpe", "maxdd", "since2024", "up_years", "pf", "win_rate")})
for bps in (80, 160):  # on_change policy trades less: the fairer Kraken case
    sim = t.simulate(px, W, S, E, bps, "next_open", "on_change")
    r = summary(sim["ret"], sim); OUT["sens"][f"next_open_on_change/{bps}"] = r
    p("on_change", bps, {k: r[k] for k in ("cagr", "sharpe", "maxdd", "since2024", "turnover_x_per_yr")})

# 3. Cash yield
sim = t.simulate(px, W, S, E, 10, "next_open", "target", cash="tbill")
OUT["cash_tbill"] = summary(sim["ret"], sim)
OUT["BH_rebal_x055_tbill"] = summary(t.bench(px, S, E, "rebal", exposure=OUT["cash_tbill"]["exposure"], cash="tbill"))
p("tbill cash", OUT["cash_tbill"]["cagr"], OUT["cash_tbill"]["sharpe"], "exposure-matched BH w/ tbill", OUT["BH_rebal_x055_tbill"]["cagr"], OUT["BH_rebal_x055_tbill"]["sharpe"])

# 4. Start-date sensitivity
p("== 4. start dates ==")
OUT["starts"] = {}
for st in ("2018-01-01", "2019-01-01", "2020-01-01", "2021-01-01", "2021-11-10", "2022-01-01", "2023-01-01", "2024-01-01"):
    sim = t.simulate(px, W, st, E, 10)
    a = summary(sim["ret"], sim); b = summary(t.bench(px, st, E, "rebal"))
    OUT["starts"][st] = dict(t6=a, bh=b); p(st, "T6", a["cagr"], a["sharpe"], a["maxdd"], "| BH", b["cagr"], b["sharpe"], b["maxdd"])
OUT["year_2018"] = dict(t6=summary(t.simulate(px, W, "2018-01-01", "2018-12-31", 10)["ret"]), bh=summary(t.bench(px, "2018-01-01", "2018-12-31")))
p("2018 alone", OUT["year_2018"]["t6"]["cagr"], OUT["year_2018"]["bh"]["cagr"], OUT["year_2018"]["t6"]["maxdd"], OUT["year_2018"]["bh"]["maxdd"])

# 5. Simple SMA200 rule and single-signal variants
p("== 5. SMA200 single rule ==")
OUT["simple"] = {}
for name, fn in [("sma200", t.signals_sma200)] + [
        (f"sma{n}", (lambda n: lambda c: (c > c.rolling(n).mean()).astype(float).where(c.rolling(200).mean().notna()))(n)) for n in (50, 100)] + [
        (f"mom{k}", (lambda k: lambda c: (c / c.shift(k) > 1).astype(float).where(c.rolling(200).mean().notna()))(k)) for k in (7, 14, 28)]:
    Wn = t.target_weights(px, fn)
    for bps in (10, 80):
        sim = t.simulate(px, Wn, S, E, bps)
        r = summary(sim["ret"], sim); OUT["simple"][f"{name}/{bps}"] = r
        p(name, bps, {k: r[k] for k in ("cagr", "sharpe", "maxdd", "since2024", "up_years", "exposure", "n")})
    if name == "sma200":
        rr = summary(t.simulate(px, Wn, "2018-01-01", E, 10)["ret"]); OUT["simple"]["sma200/10/from2018"] = rr
        rv = t.target_weights(px_all["vision"], fn); rv = summary(t.simulate(px_all["vision"], rv, S, E, 10)["ret"]); OUT["simple"]["sma200/10/vision"] = rv

# 6. Luck tests (coinbase, 10 bps, next_open, target)
p("== 6. luck tests ==")
base = t.simulate(px, W, S, E, 10)
real_sh = t.metrics(base["ret"])["sharpe"]
bh_ret = t.bench(px, S, E)
real_ex = real_sh - t.metrics(bh_ret)["sharpe"]
N = 1000
Wfull = W.copy()
# 6a circular shift of the whole weight path (original method), Sharpe
widx = W.loc[S:E].index
vals = W.loc[S:E].values
sh_shift = []
for _ in range(N):
    k = rng.integers(30, len(vals) - 30)
    Ws = W.copy(); Ws.loc[widx] = np.roll(vals, k, axis=0)
    sh_shift.append(t.metrics(t.simulate(px, Ws, S, E, 10)["ret"])["sharpe"])
sh_shift = np.array(sh_shift)
OUT["luck_circular_shift"] = dict(p=float((sh_shift >= real_sh).mean()), null_mean=float(sh_shift.mean()), null_p95=float(np.percentile(sh_shift, 95)), real=float(real_sh))
p("circular shift", OUT["luck_circular_shift"])
# 6b run-order permutation per coin (keeps each coin's set of holding runs: exposure + turnover identical)
def runs(v):
    out = []; s = 0
    for i in range(1, len(v) + 1):
        if i == len(v) or abs(v[i] - v[s]) > 1e-12:
            out.append(v[s:i]); s = i
    return out
R0 = [runs(vals[:, j]) for j in range(2)]
sh_runs = []
for _ in range(N):
    cols = []
    for j in range(2):
        rr = R0[j][:]; order = rng.permutation(len(rr)); cols.append(np.concatenate([rr[o] for o in order]))
    Ws = W.copy(); Ws.loc[widx] = np.column_stack(cols)
    sh_runs.append(t.metrics(t.simulate(px, Ws, S, E, 10)["ret"])["sharpe"])
sh_runs = np.array(sh_runs)
OUT["luck_run_permutation"] = dict(p=float((sh_runs >= real_sh).mean()), null_mean=float(sh_runs.mean()), null_p95=float(np.percentile(sh_runs, 95)), real=float(real_sh))
p("run permutation", OUT["luck_run_permutation"])
# 6c stationary block bootstrap of joint daily open-to-open returns -> synthetic prices; statistic = Sharpe(T6) - Sharpe(BH)
P = pd.DataFrame({c: px[c]["open"] for c in t.COINS}).loc["2018-01-01":E]
lr = np.log(P / P.shift(1)).dropna().values
ix = pd.date_range("2018-01-01", E, freq="D")
def sb_path(mean_block):
    n = len(ix); out = np.empty((n, 2)); i = rng.integers(len(lr))
    for d in range(n):
        if rng.random() < 1 / mean_block: i = rng.integers(len(lr))
        out[d] = lr[i % len(lr)]; i += 1
    return out
OUT["luck_block_bootstrap"] = {}
for mb in (5, 20):
    ex = []
    for _ in range(N // 2):
        path = 100 * np.exp(np.cumsum(sb_path(mb), 0))
        sp = {c: pd.DataFrame({"open": path[:, j], "close": np.r_[path[1:, j], path[-1, j]]}, index=ix) for j, c in enumerate(t.COINS)}
        Wb = t.target_weights(sp)
        ex.append(t.metrics(t.simulate(sp, Wb, S, E, 10)["ret"])["sharpe"] - t.metrics(t.bench(sp, S, E))["sharpe"])
    ex = np.array(ex)
    OUT["luck_block_bootstrap"][f"mean_block_{mb}d"] = dict(p=float((ex >= real_ex).mean()), null_mean=float(ex.mean()), null_p95=float(np.percentile(ex, 95)), real_excess_sharpe=float(real_ex))
    p("block bootstrap", mb, OUT["luck_block_bootstrap"][f"mean_block_{mb}d"])
# 6d alpha vs exposure-matched B&H: OLS of T6 daily returns on BH daily returns, Newey-West t on intercept
y = base["ret"].values; x = bh_ret.values
X = np.column_stack([np.ones_like(x), x]); beta = np.linalg.lstsq(X, y, rcond=None)[0]; u = y - X @ beta
L = 10; n = len(y); XtXi = np.linalg.inv(X.T @ X); S0 = (X * u[:, None]).T @ (X * u[:, None])
for l in range(1, L + 1):
    w = 1 - l / (L + 1); G = (X[l:] * u[l:, None]).T @ (X[:-l] * u[:-l, None]); S0 += w * (G + G.T)
se = np.sqrt(np.diag(XtXi @ S0 @ XtXi))
OUT["alpha_regression"] = dict(alpha_ann_pct=float(beta[0] * 365 * 100), beta=float(beta[1]), t_alpha=float(beta[0] / se[0]))
p("alpha regression", OUT["alpha_regression"])
# 6e same luck tests for the vision data (circular shift only, quick)
pxv = px_all["vision"]; Wv = t.target_weights(pxv); rv = t.metrics(t.simulate(pxv, Wv, S, E, 10)["ret"])["sharpe"]
vv = Wv.loc[S:E].values; wi = Wv.loc[S:E].index; shv = []
for _ in range(N):
    k = rng.integers(30, len(vv) - 30); Ws = Wv.copy(); Ws.loc[wi] = np.roll(vv, k, axis=0)
    shv.append(t.metrics(t.simulate(pxv, Ws, S, E, 10)["ret"])["sharpe"])
OUT["luck_circular_shift_vision"] = dict(p=float((np.array(shv) >= rv).mean()), real=float(rv))
p("circular shift vision", OUT["luck_circular_shift_vision"])
OUT["position_changes_per_coin_per_year"] = {k: round(float(v), 1) for k, v in t.changes_per_year(W, S, E).items()}
p("changes/yr", OUT["position_changes_per_coin_per_year"])
json.dump(OUT, open("verify.json", "w"), indent=1, default=str)

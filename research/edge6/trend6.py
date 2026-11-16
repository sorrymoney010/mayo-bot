"""Independent re-implementation of TREND-6 (written from the spec, not from research/edge5/ensemble.py).

Spec: BTC+ETH spot, long-or-cash. After each 00:00 UTC daily close t compute, per coin, six binary signals:
close_t > SMA50/100/200 (SMAs include close_t), close_t/close_{t-k} - 1 > 0 for k = 7, 14, 28.
Target weight per coin = 0.5 * (yes / 6). Trade at the next open (t+1). Cost charged on traded notional
(|target - drifted holding|) at half the round-trip cost per side.
"""
import numpy as np
import pandas as pd

COINS = ("BTC", "ETH")
# Approx. 3m T-bill yield by year (annual, %): used only for the "cash yield" sensitivity.
TBILL = {2017: 0.9, 2018: 1.9, 2019: 2.1, 2020: 0.4, 2021: 0.05, 2022: 2.0, 2023: 5.0, 2024: 5.0, 2025: 4.2, 2026: 3.8}


def signals_trend6(close):
    s = [close > close.rolling(n).mean() for n in (50, 100, 200)]
    s += [close / close.shift(k) - 1 > 0 for k in (7, 14, 28)]
    cnt = sum(x.astype(float) for x in s)
    cnt[close.rolling(200).mean().isna()] = np.nan  # need full warmup
    return cnt / 6.0


def signals_sma200(close):
    w = (close > close.rolling(200).mean()).astype(float)
    w[close.rolling(200).mean().isna()] = np.nan
    return w


def target_weights(px, sigfn=signals_trend6, per_coin=0.5):
    return pd.DataFrame({c: per_coin * sigfn(px[c]["close"]) for c in COINS})


def simulate(px, W, start, end, rt_bps=10.0, mode="next_open", policy="target", cash="zero"):
    """W: target weight decided at close of day t (index t).
    mode: next_open (trade at open t+1), close (trade at close t), delay1 (open t+2),
          lookahead_bug (weight from close t applied over close t-1 -> close t; deliberately wrong).
    policy: target = trade to exact target daily (also trades drift); on_change = trade a coin only when its
            target changes; fixed = textbook approximation r = sum(w*r) - cost*|dw| (no drift).
    Returns dict with daily portfolio returns and per-coin contributions (index = holding day)."""
    side = rt_bps / 2 / 1e4
    if mode in ("next_open", "delay1"):
        P = pd.DataFrame({c: px[c]["open"] for c in COINS})
        lag = 1 if mode == "next_open" else 2
        Wexec = W.shift(lag)            # weight in force from open d (decided at close d-lag)
        R = P.shift(-1) / P - 1         # open d -> open d+1
    elif mode == "close":
        P = pd.DataFrame({c: px[c]["close"] for c in COINS})
        Wexec = W                        # decided and traded at close d, held close d -> close d+1
        R = P.shift(-1) / P - 1
    elif mode == "lookahead_bug":
        P = pd.DataFrame({c: px[c]["close"] for c in COINS})
        Wexec = W.shift(-1)              # uses close d+1 info for the close d -> d+1 return
        R = P.shift(-1) / P - 1
    else:
        raise ValueError(mode)
    idx = R.loc[start:end].index
    R = R.loc[idx].fillna(0.0).values
    Wt = Wexec.loc[idx].fillna(0.0).values
    rc = np.array([TBILL.get(d.year, 0) / 100 / 365 if cash == "tbill" else 0.0 for d in idx])
    n, k = R.shape
    port = np.zeros(n); contrib = np.zeros((n, k)); held = np.zeros((n, k)); costs = np.zeros((n, k))
    h = np.zeros(k)  # current weights (fraction of equity) just before trading on day d
    prev_target = np.full(k, -1.0)
    for d in range(n):
        tgt = Wt[d]
        if policy == "on_change":
            new = np.where(np.abs(tgt - prev_target) > 1e-12, tgt, h)
        else:
            new = tgt
        if policy == "fixed":
            tr = np.abs(tgt - np.where(prev_target < 0, 0, prev_target))
        else:
            tr = np.abs(new - h)
        prev_target = tgt.copy()
        c = side * tr
        costs[d] = c
        cash_w = 1 - new.sum()
        g = new * R[d]
        r = g.sum() + cash_w * rc[d] - c.sum()
        port[d] = r; contrib[d] = g - c; held[d] = new
        # drift
        if policy == "fixed":
            h = new.copy()
        else:
            h = new * (1 + R[d]) / (1 + r) if (1 + r) > 0 else np.zeros(k)
    return dict(idx=idx, ret=pd.Series(port, idx), contrib=contrib, held=held, costs=costs,
                turnover=pd.Series((costs.sum(1) / side) if side > 0 else 0, idx))


def bench(px, start, end, kind="rebal", exposure=1.0, cash="zero"):
    P = pd.DataFrame({c: px[c]["open"] for c in COINS})
    R = (P.shift(-1) / P - 1).loc[start:end].fillna(0.0)
    rc = pd.Series([TBILL.get(d.year, 0) / 100 / 365 if cash == "tbill" else 0.0 for d in R.index], R.index)
    if kind == "rebal":
        return exposure * R.mean(1) + (1 - exposure) * rc
    if kind == "hold":  # 50/50 bought on day 1, never rebalanced
        eq = (1 + R).cumprod().mean(1)
        return eq.pct_change().fillna(eq.iloc[0] - 1)
    if kind in COINS:
        return R[kind]
    raise ValueError(kind)


def metrics(ret):
    eq = (1 + ret).cumprod()
    yrs = len(ret) / 365.25
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    sh = ret.mean() / ret.std() * np.sqrt(365) if ret.std() > 0 else 0
    dd = (eq / eq.cummax() - 1).min()
    yearly = (1 + ret).groupby(ret.index.year).prod() - 1
    s24 = (1 + ret.loc["2024-01-01":]).prod() - 1
    return dict(cagr=cagr, sharpe=sh, maxdd=dd, total=eq.iloc[-1] - 1, since2024=s24, yearly=yearly.to_dict())


def trades(sim):
    """A 'trade' = a maximal stretch of days with a non-zero holding in a coin (the original's definition).
    PnL = sum of that coin's daily contributions incl. entry/rebalance costs, plus the exit cost."""
    out = []
    held, contrib, costs = sim["held"], sim["contrib"], sim["costs"]
    for j, c in enumerate(COINS):
        on = held[:, j] > 1e-12
        cur = None
        for d in range(len(on)):
            if on[d]:
                if cur is None:
                    cur = dict(coin=c, start=sim["idx"][d], pnl=0.0)
                cur["pnl"] += contrib[d, j]
            elif cur is not None:
                cur["pnl"] -= costs[d, j]  # exit trade
                cur["end"] = sim["idx"][d]
                out.append(cur); cur = None
        if cur is not None:
            cur["end"] = sim["idx"][-1]; out.append(cur)
    df = pd.DataFrame(out)
    if df.empty:
        return dict(n=0)
    w = df.pnl[df.pnl > 0].sum(); l = -df.pnl[df.pnl <= 0].sum()
    return dict(n=len(df), n_btc=int((df.coin == "BTC").sum()), n_eth=int((df.coin == "ETH").sum()),
                win_rate=float((df.pnl > 0).mean()), pf=float(w / l) if l > 0 else float("inf"))


def changes_per_year(W, start, end):
    w = W.loc[start:end]
    return (w.diff().abs() > 1e-12).sum() / (len(w) / 365.25)

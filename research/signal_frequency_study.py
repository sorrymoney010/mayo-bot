"""Signal-frequency / gate-blocker study (paper research, read-only data)."""
from __future__ import annotations
import glob, gzip, json, math, sys, itertools
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dublin_bot.backtest_core import (add_indicators, regime_signals, meanrev_signals,
    trendhold_signals, adx_hysteresis, simulate, Spec, Costs)
from dublin_bot.daily_filter import riskon_table, attach_d1, d1_mask
from dublin_bot.indicators import fee_edge_ok

LIVE = Path("/workspace/mayo-bot/data"); PUB = Path("/workspace/pr12-sandbox/worktree_artifacts/data")
END = None

def load_1m(sym):
    fs = sorted(glob.glob(str(LIVE / "bars1m" / sym / "*.csv.gz")))
    df = pd.concat(pd.read_csv(f, usecols=["timestamp","open","high","low","close","volume"]) for f in fs)
    df["ts"] = pd.to_datetime(df["timestamp"], utc=True)
    return df.drop_duplicates("ts").set_index("ts").sort_index()

EXT = Path(__file__).resolve().parents[1] / "extdata"
def ext_ohlc(sym, tf):
    j = json.load(open(EXT / f"{sym}_{tf}.json"))["result"]
    rows = next(v for k, v in j.items() if k != "last")
    df = pd.DataFrame(rows, columns=["time","open","high","low","close","vwap","volume","count"]).astype(float)
    df = df.iloc[:-1]; df["time"] = df["time"].astype(np.int64)
    df.index = pd.to_datetime(df["time"], unit="s", utc=True)
    return df[["open","high","low","close","volume","time"]]

def bars(sym, tf):
    if not (LIVE / "bars1m" / sym).exists():
        return ext_ohlc(sym, tf), None
    m = load_1m(sym)
    r = m.resample(f"{tf}min", label="left", closed="left").agg(
        {"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna()
    r = r.iloc[:-1]  # drop in-progress bar
    pf = PUB / f"kraken_{sym}_{tf}m.csv"
    if pf.exists():
        p = pd.read_csv(pf); p["ts"] = pd.to_datetime(p["time"], unit="s", utc=True)
        p = p.set_index("ts")[["open","high","low","close","volume"]]
        r = pd.concat([p[p.index < r.index[0]], r])
    r["time"] = (r.index.asi8 // 10**9).astype(np.int64)
    return r, m

def daily_table(sym, m):
    if m is None:
        return ext_ohlc(sym, 1440)[["time","close"]].reset_index(drop=True)
    k = pd.read_csv(LIVE / f"kraken_{sym}_1440m.csv")[["time","close"]]
    dd = m["close"].resample("1D").last().dropna().iloc[:-1]
    ext = pd.DataFrame({"time": dd.index.asi8 // 10**9, "close": dd.values})
    k = pd.concat([k, ext[ext.time > k.time.max()]])
    return k

def frame(sym, tf):
    r, m = bars(sym, tf)
    d = add_indicators(r)
    k = daily_table(sym, m)
    now = float(d["time"].iloc[-1] + tf*60 + 1)
    return attach_d1(d, riskon_table(k, now=now), tf)

def lcb(x):
    x = np.asarray(x); n = len(x)
    if n < 2: return float("nan")
    return float(x.mean() - 1.645 * x.std(ddof=1) / math.sqrt(n))

def stats(nets):
    x = np.sort(np.asarray(nets))
    n = len(x)
    if n == 0: return dict(n=0)
    ex2 = x[:-2].mean() if n > 2 else float("nan")
    return dict(n=n, mean_bps=round(x.mean()*1e4,1), lcb_bps=round(lcb(x)*1e4,1),
                ex_best2_bps=round(ex2*1e4,1), win=round((x>0).mean(),2),
                passes=bool(n>=30 and x.mean()>0 and lcb(x)>0 and ex2>0))

# ---------- gate accounting (part 1) ----------
def gates(d, strat, p):
    c = d["close"].to_numpy(float)
    if strat == "regime":
        s = regime_signals(d, {**p, "d1": False})
        comp = {"adx": s["adx_on"], "ema200": c > d["ema200"].to_numpy(float),
                "ema20>50": d["ema20"].to_numpy(float) > d["ema50"].to_numpy(float),
                "breakout": c > np.nan_to_num(s["prior_high"], nan=np.inf), "d1": d1_mask(d)}
        tp = p.get("tp", 0.25)
    elif strat == "meanrev":
        s = meanrev_signals(d, {**p, "d1": False})
        comp = {"rsi": s["rsi"] <= p.get("rsi_os", 38), "below_ema": c < s["ema"], "d1": d1_mask(d)}
        near_rsi = s["rsi"] <= p.get("rsi_os", 38) + 7
        tp = p.get("tp", 0.25)
    else:
        s = trendhold_signals(d, {**p, "d1": False})
        comp = {"close>ema_slow": c > s["ema_slow"], "ema_fast>slow": s["ema_fast"] > s["ema_slow"], "d1": d1_mask(d)}
        tp = 1.0
    ok_fee, _ = fee_edge_ok(tp, 100.0)
    comp["fee_edge"] = np.full(len(d), ok_fee)
    M = np.vstack(list(comp.values())); names = list(comp)
    nfail = (~M).sum(0)
    full = nfail == 0
    near = nfail == 1
    if strat == "meanrev":  # a "near" RSI miss must be within 7 pts of the threshold
        near &= ~((~comp["rsi"]) & ~near_rsi)
    return full, near, M, names

def onset(mask):  # count episodes (rising edges) rather than bars
    m = mask.astype(int); return int(((m[1:] - m[:-1]) == 1).sum() + m[0])

LABEL = {"adx":"trend/ADX","d1":"D1 daily","fee_edge":"fee min-edge"}
def part1(strat, tf, p, days):
    out = {}
    for sym in ["BTCUSD","ETHUSD","SOLUSD"]:
        d = frame(sym, tf)
        warm = 210 if strat != "trendhold" else p.get("ema_slow",100)
        full, near, M, names = gates(d, strat, p)
        t0 = d["time"].iloc[-1] - days*86400
        w = (d["time"].to_numpy() > t0) & (np.arange(len(d)) >= warm)
        cov = (d["time"].iloc[-1] - d["time"][w].iloc[0]) / 86400 if w.any() else 0
        blk = {}
        for j, nm in enumerate(names):
            bm = near & ~M[j] & w
            blk[LABEL.get(nm, "other:"+nm)] = onset(bm)
        out[sym] = dict(covered_days=round(cov,1), full_signal_bars=int((full&w).sum()),
                        full_signal_events=onset(full&w), near_bars=int((near&w).sum()),
                        near_events=onset(near&w), near_blocked_by=blk)
    return out

LIVEP = {"regime": dict(lookback=20, atr_mult=3.0, adx_enter=25.0, adx_exit=20.0, stop=0.03, tp=0.25, d1=True),
         "meanrev": dict(rsi_os=38.0, rsi_exit=55.0, ema=50, stop=0.03, tp=0.25, entry="limit", limit_offset=0.001, d1=True),
         "trendhold": dict(ema_fast=20, ema_slow=100, stop=0.0, d1=True, warm=100)}
TF = {"regime":60, "meanrev":240, "trendhold":240}

TFS = (60, 240)
def variants():
    V = []
    for strat in LIVEP:
        base = LIVEP[strat]
        grid = []
        if strat == "regime":
            for ae, ax in [(25,20),(22,18),(20,15),(18,14)]:
                for d1 in (True, False):
                    for lb in (20, 10):
                        grid.append(dict(adx_enter=ae, adx_exit=ax, d1=d1, lookback=lb))
        elif strat == "meanrev":
            for rs in (38, 40, 42, 45):
                for d1 in (True, False):
                    for ent in ("limit","market"):
                        grid.append(dict(rsi_os=rs, d1=d1, entry=ent))
        else:
            for fs in [(20,100),(20,50),(10,50)]:
                for d1 in (True, False):
                    grid.append(dict(ema_fast=fs[0], ema_slow=fs[1], d1=d1))
        tfs = [60, 240] if strat != "regime" else [60, 240]
        for g in grid:
            for tf in TFS:
                V.append((strat, tf, {**base, **g}))
    return V

SYMS = ["BTCUSD","ETHUSD","SOLUSD"]
def part2(oos_days=60, syms=None, tfs=(60,240)):
    syms = syms or SYMS
    frames = {(s,tf): frame(s,tf) for s in syms for tf in tfs}
    res = []
    for strat, tf, p in variants():
        for fee in (40, 25, 10):
            cost = Costs(fee_bps=fee, maker_bps=fee, slippage_bps=5.0)
            oos, full_n = [], 0
            per = {}
            for sym in syms:
                d = frames[(sym, tf)].reset_index(drop=True)
                tr = simulate(d, Spec(strat, p), cost)
                t0 = d["time"].iloc[-1] - oos_days*86400
                full_n += len(tr)
                o = [x.net for x in tr if x.entry_time > t0]; oos += o; per[sym] = o
            st = stats(oos)
            res.append(dict(strat=strat, tf=tf, fee=fee, params={k:p[k] for k in p if k not in ("stop","tp","limit_offset","rsi_exit","atr_mult")}, all_trades=full_n, oos=st, per_sym={k: stats(v) for k, v in per.items()}))
    return res

if __name__ == "__main__":
    p1 = {f"{s}@{TF[s]}m": {f"{n}d": part1(s, TF[s], LIVEP[s], n) for n in (30,120)} for s in LIVEP}
    print(json.dumps(p1, indent=1), flush=True)
    p2 = part2()
    json.dump({"part1":p1, "part2":p2}, open(sys.argv[1]+".p2", "w"), indent=1, default=str)
    print("P2DONE", flush=True)
    ALT = ["XRPUSD","DOGEUSD","ADAUSD","AVAXUSD","LINKUSD","LTCUSD","DOTUSD","SUIUSD"]
    p3 = part2(oos_days=60, syms=ALT)
    json.dump({"part1":p1, "part2":p2, "alts":p3}, open(sys.argv[1], "w"), indent=1, default=str)
    print(json.dumps(p1, indent=1))

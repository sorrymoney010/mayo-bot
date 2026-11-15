"""CAPIT-24 SHADOW signal log (no orders, no keys, public data only). Standalone: does not touch
paper_trader_loop.py, its lock or its book.

Frozen rule (reports/edge_search_2026-11-14.md):
  on each CLOSED 1h bar t: z24 = 24h log return / (trailing-720h stdev of 1h log returns, excl. bar t, * sqrt 24) <= -2.5
  AND 24h quote volume >= 2x (trailing-720h mean hourly quote volume ending 24h earlier * 24)
  -> hypothetical buy at open of t+1h, sell at open of t+1h+48h. Max 2 concurrent (1 per coin), ties -> most negative z.
  Each slot = 50% of the starting $466.81 (non-compounded); cost 10 bps round trip.

Data: Binance.US public REST 1h klines. Price = <COIN>USDT; volume = USDT + USD pair quote volume (Binance.US is thin,
so both pairs are summed). Binance global REST is geo-blocked (HTTP 451) from this box; if Binance.US fails for a coin
the run aborts (no partial decisions) and retries next cycle. Every record carries "source".

Catch-up safe + idempotent: a cursor (last fully decided signal bar) lives in state.json. Each run refetches the
candles and walks every bar after the cursor whose NEXT bar (the entry bar) already exists, so missed hours are
replayed in order and nothing is logged twice (signal/trade ids are also de-duplicated against the jsonl files)."""
import json, os, sys, time, math, datetime as dt, urllib.request
import numpy as np, pandas as pd
OUT = os.environ.get("CAPIT_OUT", "/workspace/mayo-bot/shadow/capit24")
COINS = "BTC ETH SOL XRP DOGE ADA AVAX LINK LTC BNB DOT TRX".split()
SEED = pd.Timestamp(os.environ.get("CAPIT_SEED", "2026-11-14T00:00:00Z"))   # forward test starts here; holdout trades excluded
Z, VM, N, HOLD, MAXPOS = 2.5, 2.0, 24, 48, 2
START_EQ, SLOT, COST = 466.81, 0.5 * 466.81, 0.0010
H = pd.Timedelta("1h")
def now(): return pd.Timestamp.now(tz="UTC")
def http(url):
    req = urllib.request.Request(url, headers={"User-Agent": "mayo-capit-shadow/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r: return json.load(r)
HOURS = int(os.environ.get("CAPIT_HOURS", "1100"))
def klines(sym, hours=None):
    hours = hours or HOURS
    rows, start = [], int((now() - pd.Timedelta(hours=hours)).timestamp() * 1000)
    while True:
        d = http(f"https://api.binance.us/api/v3/klines?symbol={sym}&interval=1h&startTime={start}&limit=1000")
        if isinstance(d, dict): raise RuntimeError(f"{sym}: {d}")
        rows += d
        if len(d) < 1000: break
        start = d[-1][0] + 3600_000
    df = pd.DataFrame(rows).iloc[:, :8]; df.columns = "t o h l c v ct qv".split()
    df["t"] = pd.to_datetime(df.t, unit="ms", utc=True)
    df = df.set_index("t")[["o", "h", "l", "c", "qv", "ct"]].astype(float)
    return df[~df.index.duplicated()].sort_index()
def load_coin(c):
    px = klines(f"{c}USDT")
    try: qv2 = klines(f"{c}USD")["qv"]
    except Exception: qv2 = pd.Series(0.0, index=px.index)
    px["qv"] = px["qv"] + qv2.reindex(px.index).fillna(0.0)
    closed = px[px.ct / 1000 < now().timestamp()]          # closed bars
    lv = np.log(closed.c); sd = lv.diff().rolling(720, min_periods=360).std().shift(1)
    z = lv.diff(N) / (sd * math.sqrt(N))
    volr = closed.qv.rolling(N).sum() / (closed.qv.rolling(720, min_periods=360).mean().shift(N) * N)
    return px, closed, z, volr
def jl_ids(path, key="id"):
    if not os.path.exists(path): return set()
    return {json.loads(l)[key] for l in open(path) if l.strip()}
def append(path, rec):
    with open(path, "a") as f: f.write(json.dumps(rec) + "\n")
def run():
    os.makedirs(OUT, exist_ok=True)
    sp, tp, stp = f"{OUT}/state.json", f"{OUT}/signals.jsonl", f"{OUT}/trades.jsonl"
    st = json.load(open(sp)) if os.path.exists(sp) else {"cursor": (SEED - H).isoformat(), "open": []}
    cursor = pd.Timestamp(st["cursor"])
    D = {c: load_coin(c) for c in COINS}   # raises -> no partial decisions this cycle
    sig_ids, trade_ids = jl_ids(tp), jl_ids(stp)
    # bars decidable: signal bar t closed AND entry bar t+1h present for all coins (entry open known)
    last_open = min(D[c][0].index.max() for c in COINS)
    t = cursor + H
    while t + H <= last_open:
        open_pos = list(st["open"]); before = list(open_pos)
        # 1) close positions whose exit bar open is known (exit <= t+1h); do this in time order
        still = []
        for p in open_pos:
            x = pd.Timestamp(p["exit"])
            if x <= t + H and x in D[p["coin"]][0].index:
                xp = float(D[p["coin"]][0].o.loc[x]); g = xp / p["px"] - 1; net = g - COST
                rec = dict(id=p["id"], coin=p["coin"], signal_bar=p["signal_bar"], entry=p["entry"], exit=p["exit"], px=p["px"],
                           xp=xp, z=p["z"], gross_bps=round(1e4 * g, 1), net_bps=round(1e4 * net, 1),
                           pnl_usd=round(SLOT * net, 2), source="binanceus", logged_at=now().isoformat())
                if rec["id"] not in trade_ids: append(stp, rec); trade_ids.add(rec["id"])
            else: still.append(p)
        open_pos = still
        # 2) signals on bar t (backtest rule: a position still counts as open while its exit > t)
        cands = []
        for c in COINS:
            px, closed, z, volr = D[c]
            if t not in closed.index or pd.isna(z.get(t)) or pd.isna(volr.get(t)): continue
            if z.loc[t] <= -Z and volr.loc[t] >= VM: cands.append((float(z.loc[t]), c, float(volr.loc[t])))
        held = [p for p in before if pd.Timestamp(p["exit"]) > t]
        for zz, c, vr in sorted(cands):
            sid = f"{c}|{t.isoformat()}"; e = t + H; taken, why = False, ""
            if len(held) >= MAXPOS: why = "max_pos"
            elif any(p["coin"] == c for p in held): why = "coin_already_held"
            else:
                pxe = float(D[c][0].o.loc[e]); p = dict(id=sid, coin=c, signal_bar=t.isoformat(), entry=e.isoformat(),
                    exit=(e + pd.Timedelta(hours=HOLD)).isoformat(), px=pxe, z=round(zz, 3))
                held.append(p); open_pos.append(p); taken = True
            if sid not in sig_ids:
                append(tp, dict(id=sid, coin=c, signal_bar=t.isoformat(), z24=round(zz, 3), vol_ratio=round(vr, 2),
                                close=float(D[c][1].c.loc[t]), taken=taken, skip_reason=why, source="binanceus", logged_at=now().isoformat()))
                sig_ids.add(sid)
        st["open"] = open_pos; st["cursor"] = t.isoformat(); t += H
    # stats
    tr = [json.loads(l) for l in open(stp)] if os.path.exists(stp) else []
    nets = np.array([r["net_bps"] for r in tr]) / 1e4
    gp, gl = nets[nets > 0].sum(), -nets[nets < 0].sum()
    status = dict(last_run=now().isoformat(), last_run_local=now().tz_convert("America/Chicago").isoformat(),
                  cursor_signal_bar=st["cursor"], source="binanceus (USDT price, USD+USDT volume)", seed=SEED.isoformat(),
                  open_positions=st["open"], closed_count=len(tr), win_rate=(round(float((nets > 0).mean()), 3) if len(tr) else None),
                  pf=(round(float(gp / gl), 3) if gl > 0 else (None if not len(tr) else "inf")),
                  net_bps_total=round(1e4 * float(nets.sum()), 1), net_bps_mean=(round(1e4 * float(nets.mean()), 1) if len(tr) else None),
                  equity_usd=round(START_EQ + sum(r["pnl_usd"] for r in tr), 2), signals_logged=len(sig_ids),
                  progress=f"{len(tr)}/30 forward trades")
    json.dump(st, open(sp + ".tmp", "w"), indent=1); os.replace(sp + ".tmp", sp)
    json.dump(status, open(f"{OUT}/status.json.tmp", "w"), indent=1); os.replace(f"{OUT}/status.json.tmp", f"{OUT}/status.json")
    print(now().isoformat(), "ok cursor", st["cursor"], "open", len(st["open"]), "closed", len(tr), "signals", len(sig_ids), flush=True)
if __name__ == "__main__":
    import fcntl
    os.makedirs(OUT, exist_ok=True); _lk = open(f"{OUT}/cycle.lock", "w")
    try: fcntl.flock(_lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError: print(now().isoformat(), "another cycle is running; skip", flush=True); sys.exit(0)
    try: run()
    except Exception as e:
        print(now().isoformat(), "ERROR", repr(e), flush=True); sys.exit(1)

"""CAPIT-24 SHADOW signal log (no orders, no keys, public data only). Standalone: does not touch
paper_trader_loop.py, its lock or its book.

Frozen rule (reports/edge_search_2026-11-14.md):
  on each CLOSED 1h bar t: z24 = 24h log return / (trailing-720h stdev of 1h log returns, excl. bar t, * sqrt 24) <= -2.5
  AND 24h quote volume >= 2x (trailing-720h mean hourly quote volume ending 24h earlier * 24)
  -> hypothetical buy at open of t+1h, sell at open of t+1h+48h. Max 2 concurrent (1 per coin), ties -> most negative z.
  Each slot = 50% of the starting $466.81 (non-compounded); cost 10 bps round trip.

Three variants are tracked side by side (each has its own positions, signals, trades):
  base     — the frozen CAPIT-24 rule above
  r2       — same, only when BTC's regime label is not 'bear' (research/edge3/c3_capit_regime.py /
             gates.py: ADX14 < 20 => flat; else bull if close > rising EMA200, bear if close < falling
             EMA200, else flat; a new label takes effect after 3 consecutive daily bars; label at
             decision time t+1h is the last daily bar that closed before then)
  oiflush  — CAPIT-24 plus OI-flush confirmation (research/edge3/c1_oi_liq.py, frozen q=0.10 hold=48):
             24h change in Binance USDT-M OI *value* is in the trailing-90d 10% quantile (rq win=2160h,
             minp=720, shifted 1 bar — no lookahead). Public data.binance.vision daily metrics, no keys.
             Daily files publish after the UTC day closes, so this variant may lag up to ~1 day and
             catch up when the file appears.

Data: Binance.US public REST 1h klines. Price = <COIN>USDT; volume = USDT + USD pair quote volume (Binance.US is thin,
so both pairs are summed). Binance global REST is geo-blocked (HTTP 451) from this box; if Binance.US fails for a coin
the run aborts (no partial decisions) and retries next cycle. Every record carries "source".

Catch-up safe + idempotent: a cursor (last fully decided signal bar) lives in state.json, per variant.
Each run refetches the candles and walks every bar after the cursor whose NEXT bar (the entry bar)
already exists, so missed hours are replayed in order and nothing is logged twice (signal/trade ids
are also de-duplicated against the jsonl files). The base cursor and open positions are preserved
in the original state.json fields."""
import json, os, sys, time, math, datetime as dt, urllib.request, urllib.error, zipfile, io, pickle
import numpy as np, pandas as pd
OUT = os.environ.get("CAPIT_OUT", "/workspace/mayo-bot/shadow/capit24")
COINS = "BTC ETH SOL XRP DOGE ADA AVAX LINK LTC BNB DOT TRX".split()
SEED = pd.Timestamp(os.environ.get("CAPIT_SEED", "2026-11-14T00:00:00Z"))   # forward test starts here; holdout trades excluded
Z, VM, N, HOLD, MAXPOS = 2.5, 2.0, 24, 48, 2
START_EQ, SLOT = 466.81, 0.5 * 466.81
COST_BUS, COST_KRK_MAKER, COST_KRK_TAKER = 0.0010, 0.0080, 0.0170
COST = COST_BUS  # net_bps / pnl_usd in trade rows stay on Binance.US 10 bps, as before
H = pd.Timedelta("1h")
OI_Q, OI_WIN, OI_MINP, OI_LOOKBACK_D = 0.10, 2160, 720, 120
VISION = "https://data.binance.vision/data/futures/um/daily/metrics"
METRICS_CACHES = [
    os.path.join(OUT, "metrics"),
    os.environ.get("CAPIT_METRICS", "/workspace/edge2-data/metrics"),
]
VARIANTS = ("base", "r2", "oiflush")
LABELS = {"base": "CAPIT-24", "r2": "CAPIT-24|R2", "oiflush": "CAPIT-24+OI-flush"}

def now(): return pd.Timestamp.now(tz="UTC")
def http(url):
    req = urllib.request.Request(url, headers={"User-Agent": "mayo-capit-shadow/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r: return json.load(r)
def http_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": "mayo-capit-shadow/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r: return r.read()
HOURS = int(os.environ.get("CAPIT_HOURS", "1100"))
def klines(sym, hours=None, interval="1h"):
    hours = hours or HOURS
    rows, start = [], int((now() - pd.Timedelta(hours=hours)).timestamp() * 1000)
    while True:
        d = http(f"https://api.binance.us/api/v3/klines?symbol={sym}&interval={interval}&startTime={start}&limit=1000")
        if isinstance(d, dict): raise RuntimeError(f"{sym}: {d}")
        rows += d
        if len(d) < 1000: break
        # interval step: 1h = 3600_000, 1d = 86400_000
        step = 3600_000 if interval == "1h" else 86400_000
        start = d[-1][0] + step
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
def paths(variant):
    suf = "" if variant == "base" else f"_{variant}"
    return f"{OUT}/signals{suf}.jsonl", f"{OUT}/trades{suf}.jsonl"

# ── BTC regime (R2), copied from research/edge3/gates.py (daily, 3-bar persistence) ──
def adx(d, n=14):
    up = d.h.diff(); dn = -d.l.diff()
    pdm = np.where((up > dn) & (up > 0), up, 0.0); ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = np.maximum(d.h - d.l, np.maximum((d.h - d.c.shift()).abs(), (d.l - d.c.shift()).abs()))
    a = lambda x: pd.Series(x, index=d.index).ewm(alpha=1 / n, adjust=False).mean()
    atr = a(tr); pdi = 100 * a(pdm) / atr; ndi = 100 * a(ndm) / atr
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi); return dx.ewm(alpha=1 / n, adjust=False).mean()
def btc_regime_daily():
    # ~1000 calendar days covers EMA200 warm-up with ample slack
    d = klines("BTCUSDT", hours=1000 * 24, interval="1d")
    d = d[d.ct / 1000 < now().timestamp()][["o", "h", "l", "c"]]
    e = d.c.ewm(span=200, adjust=False).mean(); ax = adx(d)
    raw = np.where(ax < 20, "flat", np.where((d.c > e) & (e.diff() > 0), "bull",
           np.where((d.c < e) & (e.diff() < 0), "bear", "flat")))
    lab, cur, run, prev = [], "flat", 0, None
    for r in raw:
        run = run + 1 if r == prev else 1; prev = r
        if r != cur and run >= 3: cur = r
        lab.append(cur)
    s = pd.Series(lab, index=d.index)
    s[d.index < d.index[0] + pd.Timedelta(days=200)] = "warmup"
    return s
def regime_at_ts(reg_daily, ts):
    """Label in force at ts = label on the last daily bar that CLOSED before ts."""
    s2 = reg_daily.copy(); s2.index = s2.index + pd.Timedelta("1D")
    v = s2.reindex(pd.DatetimeIndex([ts]), method="ffill")
    x = v.iloc[0]
    return None if pd.isna(x) else str(x)

# ── Binance USDT-M OI (public vision archive; no keys; live fapi is geo-blocked) ──
def _is_zip(path):
    return os.path.isfile(path) and os.path.getsize(path) > 0 and zipfile.is_zipfile(path)
def _metrics_existing(coin, day):
    name = f"{coin}USDT-metrics-{day.isoformat()}.zip"
    today = now().date()
    for root in METRICS_CACHES:
        if not root: continue
        p = os.path.join(root, name)
        if os.path.exists(p):
            if _is_zip(p): return p
            # empty / HTML / truncated leftover — do not treat as a hit
            try: os.remove(p)
            except OSError: pass
        # Remember 404s only for dates older than yesterday; today's (and
        # yesterday's, until the archive is published) file can still appear.
        if os.path.exists(p + ".404"):
            if day < today - dt.timedelta(days=1):
                return False
            try: os.remove(p + ".404")
            except OSError: pass
    return None
def _metrics_ensure(coin, day):
    found = _metrics_existing(coin, day)
    if found is not None: return found if found else None
    dest_dir = METRICS_CACHES[0]; os.makedirs(dest_dir, exist_ok=True)
    name = f"{coin}USDT-metrics-{day.isoformat()}.zip"
    dest = os.path.join(dest_dir, name)
    url = f"{VISION}/{coin}USDT/{name}"
    tmp = dest + ".tmp"
    try:
        open(tmp, "wb").write(http_bytes(url))
        if not _is_zip(tmp):
            try: os.remove(tmp)
            except OSError: pass
            return None
        os.replace(tmp, dest); return dest
    except urllib.error.HTTPError as e:
        try:
            if os.path.exists(tmp): os.remove(tmp)
        except OSError: pass
        if e.code == 404:
            # only persist 404 for days that should already have been published
            if day < now().date() - dt.timedelta(days=1):
                open(dest + ".404", "w").close()
            return None
        raise
    except Exception:
        try:
            if os.path.exists(tmp): os.remove(tmp)
        except OSError: pass
        raise
def _read_oi_zip(path):
    z = zipfile.ZipFile(path); df = pd.read_csv(io.BytesIO(z.read(z.namelist()[0])))
    t = pd.to_datetime(df["create_time"], utc=True)
    s = pd.Series(df["sum_open_interest_value"].astype(float).values, index=t).sort_index()
    return s[~s.index.duplicated(keep="last")]
def load_oi_hourly():
    """Hourly OI value (last 5m print in [H, H+1h)), plus last complete UTC day loaded."""
    start = (SEED - pd.Timedelta(days=OI_LOOKBACK_D)).normalize()
    today = now().normalize()
    cache_p = f"{OUT}/oi_1h.pkl"
    cached = None
    if os.path.exists(cache_p):
        try: cached = pickle.load(open(cache_p, "rb"))
        except Exception: cached = None
    have_through = cached.get("through") if cached else None
    need_from = start if have_through is None else (pd.Timestamp(have_through) + pd.Timedelta("1D"))
    if need_from.tzinfo is None: need_from = need_from.tz_localize("UTC")
    panel = {c: (cached["panel"][c] if cached and c in cached.get("panel", {}) else pd.Series(dtype=float)) for c in COINS}
    last_ok = pd.Timestamp(have_through) if have_through else (start - pd.Timedelta("1D"))
    if last_ok.tzinfo is None: last_ok = last_ok.tz_localize("UTC")
    missing_today = False
    d = need_from
    while d < today + pd.Timedelta("1D"):
        day = d.date()
        any_ok, any_err = False, False
        for c in COINS:
            try:
                p = _metrics_ensure(c, day)
            except Exception as e:
                print(now().isoformat(), "OI download error", c, day, repr(e), flush=True)
                any_err = True; continue
            if not p:
                continue
            try:
                s = _read_oi_zip(p)
                prev = panel[c]
                panel[c] = pd.concat([prev, s]) if len(prev) else s
                any_ok = True
            except Exception as e:
                print(now().isoformat(), "OI zip read error", c, day, repr(e), flush=True)
                any_err = True
        if any_err:
            break
        if any_ok:
            last_ok = d
        elif d.normalize() >= today:
            missing_today = True
            break
        else:
            # a past day with no file and no error: treat as unavailable and stop so we don't skip a hole
            if d >= start:
                break
        d += pd.Timedelta("1D")
    hourly = {}
    for c in COINS:
        s = panel[c]
        if s is None or len(s) == 0:
            hourly[c] = pd.Series(dtype=float); continue
        s = s[~s.index.duplicated(keep="last")].sort_index()
        # last print within [H, H+1h) — same as research/edge3/load_metrics.py
        hourly[c] = s.resample("1h", label="left", closed="left").last()
    through = last_ok  # last UTC day we fully ingested
    try:
        pickle.dump({"through": through.isoformat(), "panel": panel}, open(cache_p + ".tmp", "wb"))
        os.replace(cache_p + ".tmp", cache_p)
    except OSError:
        pass
    # last decidable signal bar: last hour open of `through` (hour 23:00 on that day)
    oi_through_hour = through + pd.Timedelta(hours=23) if pd.notna(through) else None
    return hourly, through, oi_through_hour
def oi_features(hourly):
    out = {}
    for c in COINS:
        oi = hourly[c].astype(float)
        chg = oi.pct_change(24, fill_method=None)
        q = chg.rolling(OI_WIN, min_periods=OI_MINP).quantile(OI_Q).shift(1)
        out[c] = (chg, q)
    return out

def pf_of(nets):
    if len(nets) == 0: return None
    gp, gl = float(nets[nets > 0].sum()), float(-nets[nets < 0].sum())
    return (round(gp / gl, 3) if gl > 0 else "inf")
def variant_status(tr, open_pos, cursor, sig_n, extra=None):
    nets_bus = np.array([((r["gross_bps"] / 1e4) - COST_BUS) if "gross_bps" in r else r["net_bps"] / 1e4 for r in tr]) if tr else np.array([])
    # prefer stored net_bps (already bus-costed) when present
    if tr and "net_bps" in tr[0]:
        nets_bus = np.array([r["net_bps"] for r in tr]) / 1e4
    gross = np.array([r["gross_bps"] for r in tr]) / 1e4 if tr else np.array([])
    nets_km = gross - COST_KRK_MAKER if len(gross) else np.array([])
    nets_kt = gross - COST_KRK_TAKER if len(gross) else np.array([])
    st = dict(cursor_signal_bar=cursor, open_positions=open_pos, closed_count=len(tr),
              win_rate=(round(float((nets_bus > 0).mean()), 3) if len(tr) else None),
              pf=(pf_of(nets_bus) if len(tr) else None),
              pf_krk_maker=(pf_of(nets_km) if len(tr) else None),
              pf_krk_taker=(pf_of(nets_kt) if len(tr) else None),
              net_bps_total=round(1e4 * float(nets_bus.sum()), 1) if len(tr) else 0.0,
              net_bps_mean=(round(1e4 * float(nets_bus.mean()), 1) if len(tr) else None),
              equity_usd=round(START_EQ + sum(r.get("pnl_usd") or SLOT * ((r["gross_bps"] / 1e4) - COST_BUS) for r in tr), 2),
              signals_logged=sig_n, progress=f"{len(tr)}/30 forward trades")
    if extra: st.update(extra)
    return st


def close_due(v, open_pos, last_open, D, trade_path, trade_ids):
    """Close hypothetical positions whose exit bar is already on disk; no new entries."""
    still = []
    for p in open_pos:
        x = pd.Timestamp(p["exit"])
        if x <= last_open and x in D[p["coin"]][0].index:
            xp = float(D[p["coin"]][0].o.loc[x]); g = xp / p["px"] - 1; net = g - COST
            rec = dict(id=p["id"], variant=v, coin=p["coin"], signal_bar=p["signal_bar"], entry=p["entry"],
                       exit=p["exit"], px=p["px"], xp=xp, z=p["z"], gross_bps=round(1e4 * g, 1),
                       net_bps=round(1e4 * net, 1), pnl_usd=round(SLOT * net, 2),
                       source="binanceus", logged_at=now().isoformat())
            if rec["id"] not in trade_ids: append(trade_path, rec); trade_ids.add(rec["id"])
        else:
            still.append(p)
    return still

def load_state():
    sp = f"{OUT}/state.json"
    if os.path.exists(sp):
        st = json.load(open(sp))
    else:
        st = {"cursor": (SEED - H).isoformat(), "open": []}
    st.setdefault("cursor", (SEED - H).isoformat())
    st.setdefault("open", [])
    vs = st.setdefault("variants", {})
    for v in ("r2", "oiflush"):
        vs.setdefault(v, {"cursor": (SEED - H).isoformat(), "open": []})
        vs[v].setdefault("cursor", (SEED - H).isoformat())
        vs[v].setdefault("open", [])
    return st
def snapshot_state(st, v):
    if v == "base":
        return pd.Timestamp(st["cursor"]), list(st["open"])
    return pd.Timestamp(st["variants"][v]["cursor"]), list(st["variants"][v]["open"])
def store_state(st, v, cursor, open_pos):
    if v == "base":
        st["cursor"] = cursor; st["open"] = open_pos
    else:
        st["variants"][v]["cursor"] = cursor; st["variants"][v]["open"] = open_pos

def step_variant(v, t, cands, open_pos, D, trade_path, trade_ids, sig_path, sig_ids, extra_sig=None):
    """Close due exits, then consider entries. Returns (open_pos, n_new_signals)."""
    before = list(open_pos)
    still = []
    for p in open_pos:
        x = pd.Timestamp(p["exit"])
        if x <= t + H and x in D[p["coin"]][0].index:
            xp = float(D[p["coin"]][0].o.loc[x]); g = xp / p["px"] - 1; net = g - COST
            rec = dict(id=p["id"], variant=v, coin=p["coin"], signal_bar=p["signal_bar"], entry=p["entry"],
                       exit=p["exit"], px=p["px"], xp=xp, z=p["z"], gross_bps=round(1e4 * g, 1),
                       net_bps=round(1e4 * net, 1), pnl_usd=round(SLOT * net, 2),
                       source="binanceus", logged_at=now().isoformat())
            if rec["id"] not in trade_ids: append(trade_path, rec); trade_ids.add(rec["id"])
        else:
            still.append(p)
    open_pos = still
    held = [p for p in before if pd.Timestamp(p["exit"]) > t]
    n_new = 0
    for item in cands:
        zz, c, vr = item[0], item[1], item[2]
        sid = f"{c}|{t.isoformat()}"; e = t + H; taken, why = False, ""
        if sid in sig_ids:
            # already decided this bar on a prior run; do not open a second lot
            continue
        if len(held) >= MAXPOS: why = "max_pos"
        elif any(p["coin"] == c for p in held): why = "coin_already_held"
        else:
            pxe = float(D[c][0].o.loc[e]); p = dict(id=sid, coin=c, signal_bar=t.isoformat(), entry=e.isoformat(),
                exit=(e + pd.Timedelta(hours=HOLD)).isoformat(), px=pxe, z=round(zz, 3))
            held.append(p); open_pos.append(p); taken = True
        if sid not in sig_ids:
            rec = dict(id=sid, variant=v, coin=c, signal_bar=t.isoformat(), z24=round(zz, 3), vol_ratio=round(vr, 2),
                       close=float(D[c][1].c.loc[t]), taken=taken, skip_reason=why, source="binanceus",
                       logged_at=now().isoformat())
            if extra_sig and c in extra_sig: rec.update(extra_sig[c])
            elif extra_sig and "*" in extra_sig: rec.update(extra_sig["*"])
            append(sig_path, rec); sig_ids.add(sid); n_new += 1
    return open_pos, n_new

def run():
    os.makedirs(OUT, exist_ok=True)
    st = load_state()
    D = {c: load_coin(c) for c in COINS}   # raises -> no partial decisions this cycle
    last_open = min(D[c][0].index.max() for c in COINS)

    # R2 regime (failure must not block base)
    btc_reg, reg_err = None, None
    try:
        btc_reg = btc_regime_daily()
    except Exception as e:
        reg_err = repr(e)
        print(now().isoformat(), "R2 regime unavailable this cycle:", reg_err, flush=True)

    # OI panel (failure / lag must not block base or R2)
    oi_hourly, oi_through_day, oi_through_hour, oi_err, oi_feat = None, None, None, None, None
    try:
        oi_hourly, oi_through_day, oi_through_hour = load_oi_hourly()
        oi_feat = oi_features(oi_hourly)
    except Exception as e:
        oi_err = repr(e)
        print(now().isoformat(), "OI panel unavailable this cycle:", oi_err, flush=True)

    files, ids, opens, cursors = {}, {}, {}, {}
    for v in VARIANTS:
        sp, tp = paths(v)
        files[v] = (sp, tp)
        ids[v] = (jl_ids(sp), jl_ids(tp))
        cursors[v], opens[v] = snapshot_state(st, v)

    # walk from the earliest cursor that still has work
    t = min(cursors.values()) + H
    while t + H <= last_open:
        # CAPIT-24 candidates on bar t (same for every variant; filters applied below)
        cands = []
        for c in COINS:
            px, closed, z, volr = D[c]
            if t not in closed.index or pd.isna(z.get(t)) or pd.isna(volr.get(t)): continue
            if z.loc[t] <= -Z and volr.loc[t] >= VM: cands.append((float(z.loc[t]), c, float(volr.loc[t])))
        cands.sort()  # most negative z first (ties)

        # base
        if t > cursors["base"]:
            opens["base"], _ = step_variant("base", t, cands, opens["base"], D, files["base"][1], ids["base"][1],
                                            files["base"][0], ids["base"][0])
            cursors["base"] = t

        # R2: only when BTC regime at decision time (t+1h) is not bear
        if t > cursors["r2"] and btc_reg is not None:
            reg = regime_at_ts(btc_reg, t + H)
            extra = {"*": {"btc_regime": reg}}
            r2_cands = cands if (reg is not None and reg != "bear") else []
            # still log CAPIT cands that the filter rejected, so the log shows the skip
            if r2_cands:
                opens["r2"], _ = step_variant("r2", t, r2_cands, opens["r2"], D, files["r2"][1], ids["r2"][1],
                                              files["r2"][0], ids["r2"][0], extra_sig=extra)
            else:
                # record rejected CAPIT signals (filter skip) so catch-up doesn't miss the audit trail
                for zz, c, vr in cands:
                    sid = f"{c}|{t.isoformat()}"
                    if sid not in ids["r2"][0]:
                        append(files["r2"][0], dict(id=sid, variant="r2", coin=c, signal_bar=t.isoformat(),
                            z24=round(zz, 3), vol_ratio=round(vr, 2), close=float(D[c][1].c.loc[t]),
                            taken=False, skip_reason="btc_regime_bear" if reg == "bear" else "btc_regime_unknown",
                            btc_regime=reg, source="binanceus", logged_at=now().isoformat()))
                        ids["r2"][0].add(sid)
                # still close any due exits even when no new entries
                opens["r2"], _ = step_variant("r2", t, [], opens["r2"], D, files["r2"][1], ids["r2"][1],
                                              files["r2"][0], ids["r2"][0])
            cursors["r2"] = t

        # OI-flush: only decide bars whose OI day is complete
        if t > cursors["oiflush"] and oi_feat is not None and oi_through_hour is not None and t <= oi_through_hour:
            extra, oi_cands = {}, []
            for zz, c, vr in cands:
                chg, q = oi_feat[c]
                cv = chg.get(t) if t in chg.index else np.nan
                qv = q.get(t) if t in q.index else np.nan
                info = {"oi_chg_24h": (None if pd.isna(cv) else round(float(cv), 5)),
                        "oi_q10": (None if pd.isna(qv) else round(float(qv), 5))}
                extra[c] = info
                if pd.notna(cv) and pd.notna(qv) and float(cv) <= float(qv):
                    oi_cands.append((zz, c, vr))
            if oi_cands:
                opens["oiflush"], _ = step_variant("oiflush", t, oi_cands, opens["oiflush"], D,
                    files["oiflush"][1], ids["oiflush"][1], files["oiflush"][0], ids["oiflush"][0], extra_sig=extra)
                # log CAPIT-but-no-flush signals
                taken_coins = {x[1] for x in oi_cands}
                for zz, c, vr in cands:
                    if c in taken_coins: continue
                    sid = f"{c}|{t.isoformat()}"
                    if sid not in ids["oiflush"][0]:
                        rec = dict(id=sid, variant="oiflush", coin=c, signal_bar=t.isoformat(),
                                   z24=round(zz, 3), vol_ratio=round(vr, 2), close=float(D[c][1].c.loc[t]),
                                   taken=False, skip_reason="no_oi_flush", source="binanceus",
                                   logged_at=now().isoformat())
                        rec.update(extra.get(c, {}))
                        append(files["oiflush"][0], rec); ids["oiflush"][0].add(sid)
            else:
                for zz, c, vr in cands:
                    sid = f"{c}|{t.isoformat()}"
                    if sid not in ids["oiflush"][0]:
                        rec = dict(id=sid, variant="oiflush", coin=c, signal_bar=t.isoformat(),
                                   z24=round(zz, 3), vol_ratio=round(vr, 2), close=float(D[c][1].c.loc[t]),
                                   taken=False, skip_reason="no_oi_flush", source="binanceus",
                                   logged_at=now().isoformat())
                        rec.update(extra.get(c, {}))
                        append(files["oiflush"][0], rec); ids["oiflush"][0].add(sid)
                opens["oiflush"], _ = step_variant("oiflush", t, [], opens["oiflush"], D,
                    files["oiflush"][1], ids["oiflush"][1], files["oiflush"][0], ids["oiflush"][0])
            cursors["oiflush"] = t

        t += H

    # Close exits whose exit-bar open is already known, even if a variant's entry
    # cursor is lagging (OI files publish a day late; R2 skipped a cycle).
    for v in VARIANTS:
        opens[v] = close_due(v, opens[v], last_open, D, files[v][1], ids[v][1])

    for v in VARIANTS:
        store_state(st, v, cursors[v].isoformat() if hasattr(cursors[v], "isoformat") else str(cursors[v]),
                    opens[v])

    # stats
    cur_reg = regime_at_ts(btc_reg, now()) if btc_reg is not None else None
    vstatus = {}
    for v in VARIANTS:
        tp = files[v][1]
        tr = [json.loads(l) for l in open(tp)] if os.path.exists(tp) else []
        extra = {"label": LABELS[v]}
        if v == "r2":
            extra["btc_regime_now"] = cur_reg
            if reg_err: extra["regime_error"] = reg_err
        if v == "oiflush":
            extra.update(oi_q=OI_Q, hold_h=HOLD,
                         oi_source="data.binance.vision futures/um/daily/metrics (USDT-M OI value)",
                         oi_through_day=(oi_through_day.date().isoformat() if oi_through_day is not None else None),
                         oi_through_hour=(oi_through_hour.isoformat() if oi_through_hour is not None else None))
            if oi_err: extra["oi_error"] = oi_err
        vstatus[v] = variant_status(tr, opens[v],
                                    cursors[v].isoformat() if hasattr(cursors[v], "isoformat") else str(cursors[v]),
                                    len(ids[v][0]), extra)

    status = dict(last_run=now().isoformat(), last_run_local=now().tz_convert("America/Chicago").isoformat(),
                  source="binanceus (USDT price, USD+USDT volume)", seed=SEED.isoformat(),
                  variants=vstatus,
                  # backward-compatible top-level fields = base variant
                  cursor_signal_bar=vstatus["base"]["cursor_signal_bar"],
                  open_positions=vstatus["base"]["open_positions"],
                  closed_count=vstatus["base"]["closed_count"], win_rate=vstatus["base"]["win_rate"],
                  pf=vstatus["base"]["pf"], net_bps_total=vstatus["base"]["net_bps_total"],
                  net_bps_mean=vstatus["base"]["net_bps_mean"], equity_usd=vstatus["base"]["equity_usd"],
                  signals_logged=vstatus["base"]["signals_logged"], progress=vstatus["base"]["progress"])
    sp = f"{OUT}/state.json"
    json.dump(st, open(sp + ".tmp", "w"), indent=1); os.replace(sp + ".tmp", sp)
    json.dump(status, open(f"{OUT}/status.json.tmp", "w"), indent=1); os.replace(f"{OUT}/status.json.tmp", f"{OUT}/status.json")
    parts = [f"{v} open {len(opens[v])} closed {vstatus[v]['closed_count']} sig {vstatus[v]['signals_logged']}" for v in VARIANTS]
    print(now().isoformat(), "ok", " | ".join(parts), "base_cursor", st["cursor"], flush=True)

if __name__ == "__main__":
    import fcntl
    os.makedirs(OUT, exist_ok=True); _lk = open(f"{OUT}/cycle.lock", "w")
    try: fcntl.flock(_lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError: print(now().isoformat(), "another cycle is running; skip", flush=True); sys.exit(0)
    try: run()
    except Exception as e:
        print(now().isoformat(), "ERROR", repr(e), flush=True); sys.exit(1)

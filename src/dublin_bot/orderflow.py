"""Tick order-flow features: who is buying, who is selling, who is cashing out.

Pure functions over Kraken spot trade ticks (``trade_id, ts, price, qty, side``;
side ``b`` = buyer was the aggressor). Nothing here touches the network, a lock,
or an order route. Used by ``flow_sim`` (backtests), ``scripts/study_orderflow_exits.py``
and the order-flow shadow overlays.

Feature definitions (fixed before the study ran; nothing below is fitted to returns):

* aggressor ORDER = consecutive fills with the same timestamp and side (one taker order
  sweeping several resting orders). Its USD notional is the "trade size".
* LARGE order = notional above the 99th percentile of the previous ``LARGE_LOOKBACK_DAYS``
  days of orders for that coin (no look-ahead; the first day has no large orders).
* ``imb_W``      (buy USD - sell USD) / total USD over the trailing W minutes
* ``net_z_W``    net USD flow over W minutes / its trailing 7-day std
* ``lnet_z_W``   LARGE-order net USD flow over W minutes / its trailing 7-day std
* ``ret_z_W``    W-minute log return / its trailing 7-day std
* ``vwap_dev_z`` (close - 24h rolling VWAP) / close, divided by its trailing 7-day std
* 15m-grid ``vol_z`` (15m USD volume vs trailing 4 days), ``range_x`` (15m range vs
  trailing 4-day median range), ``buy_climax`` / ``sell_climax`` (volume spike + range
  expansion + new 4h extreme + close back in the opposite half of the bar)
* ``bull_abs_W``  heavy net selling (net_z <= -1) while price did not fall (ret_z >= 0)
* ``bear_abs_W``  heavy net buying (net_z >= +1) while price did not rise (ret_z <= 0)
* ``euphoria``    price stretched far above 24h VWAP (vwap_dev_z >= 2.5) while large
                  orders net SELL over the last hour (lnet_z_60 < 0): big players cashing out
"""
from __future__ import annotations

import gzip
import io
import math
from collections import deque
from pathlib import Path

import numpy as np
import pandas as pd

LARGE_Q = 0.99
LARGE_LOOKBACK_DAYS = 7
Z_WINDOW_MIN = 7 * 1440
Z_MIN_PERIODS = 1440
WINDOWS = (5, 15, 60, 240)
FINE_MIN = 15

FLOW_1M_COLS = ["open", "high", "low", "close", "volume", "buy_vol", "sell_vol", "usd",
                "buy_usd", "sell_usd", "lbuy_usd", "lsell_usd", "orders"]


# ── ticks -> 1m flow ───────────────────────────────────────────

def read_tick_file(path: Path | str) -> pd.DataFrame:
    p = Path(path)
    if p.suffix == ".gz":
        with gzip.open(p, "rb") as fh:
            raw = fh.read()
        return pd.read_csv(io.BytesIO(raw), usecols=["trade_id", "ts", "price", "qty", "side"])
    return pd.read_csv(p, usecols=["trade_id", "ts", "price", "qty", "side"])


def aggressor_orders(t: pd.DataFrame) -> tuple[pd.DataFrame, np.ndarray]:
    """Collapse fills into taker orders (same ts + same side, consecutive).

    Returns (orders, gid) where ``gid[i]`` is the order index of fill row i (rows sorted
    by trade id)."""
    if t is None or not len(t):
        return pd.DataFrame(columns=["ts", "side", "usd", "first_row", "gid"]), np.zeros(0, dtype=np.int64)
    t = t.sort_values("trade_id", kind="stable").reset_index(drop=True)
    ts = t["ts"].to_numpy(float)
    side = (t["side"].to_numpy() == "b")
    new = np.ones(len(t), dtype=bool)
    new[1:] = (ts[1:] != ts[:-1]) | (side[1:] != side[:-1])
    gid = np.cumsum(new) - 1
    usd = (t["price"].to_numpy(float) * t["qty"].to_numpy(float))
    order_usd = np.bincount(gid, weights=usd)
    first = np.nonzero(new)[0]
    return pd.DataFrame({"ts": ts[first], "side": side[first], "usd": order_usd, "first_row": first,
                         "gid": np.arange(len(first))}), gid


def ticks_to_flow_1m(t: pd.DataFrame, large_usd: float | None) -> pd.DataFrame:
    """1m flow bars (only minutes that traded). ``large_usd`` None/NaN -> no large orders."""
    if t is None or not len(t):
        return pd.DataFrame(columns=FLOW_1M_COLS)
    t = t.sort_values("trade_id", kind="stable").reset_index(drop=True)
    orders, gid = aggressor_orders(t)
    thr = float(large_usd) if large_usd is not None and math.isfinite(float(large_usd)) else math.inf
    is_large_order = orders["usd"].to_numpy(float) >= thr
    large_row = is_large_order[gid]
    price = t["price"].to_numpy(float)
    qty = t["qty"].to_numpy(float)
    usd = price * qty
    buy = t["side"].to_numpy() == "b"
    minute = (t["ts"].to_numpy(float) // 60).astype(np.int64) * 60
    df = pd.DataFrame({
        "m": minute, "price": price, "qty": qty, "usd": usd,
        "bq": np.where(buy, qty, 0.0), "sq": np.where(buy, 0.0, qty),
        "bu": np.where(buy, usd, 0.0), "su": np.where(buy, 0.0, usd),
        "lbu": np.where(buy & large_row, usd, 0.0), "lsu": np.where(~buy & large_row, usd, 0.0),
        "neworder": np.r_[True, gid[1:] != gid[:-1]].astype(np.int64),
    })
    g = df.groupby("m", sort=True)
    out = pd.DataFrame({
        "open": g["price"].first(), "high": g["price"].max(), "low": g["price"].min(),
        "close": g["price"].last(), "volume": g["qty"].sum(), "buy_vol": g["bq"].sum(),
        "sell_vol": g["sq"].sum(), "usd": g["usd"].sum(), "buy_usd": g["bu"].sum(),
        "sell_usd": g["su"].sum(), "lbuy_usd": g["lbu"].sum(), "lsell_usd": g["lsu"].sum(),
        "orders": g["neworder"].sum(),
    })
    out.index.name = "minute"
    return out[FLOW_1M_COLS]


def build_flow_1m(tick_dir: Path | str, *, start: float | None = None, end: float | None = None,
                  cache_dir: Path | str | None = None) -> pd.DataFrame:
    """1m flow for every day file in ``tick_dir`` (read-only), large threshold rolling.

    ``cache_dir`` (optional, must NOT be inside the live data dir) stores per-day pickles.
    """
    tick_dir = Path(tick_dir)
    files = sorted(p for p in tick_dir.iterdir()
                   if p.name[:4].isdigit() and (p.name.endswith(".csv.gz") or p.name.endswith(".csv")))
    hist: deque[np.ndarray] = deque(maxlen=LARGE_LOOKBACK_DAYS)
    parts = []
    cdir = Path(cache_dir) if cache_dir else None
    if cdir:
        cdir.mkdir(parents=True, exist_ok=True)
    for f in files:
        day = f.name[:10]
        thr = float(np.quantile(np.concatenate(list(hist)), LARGE_Q)) if hist else None
        cache = None
        if cdir:
            st = f.stat()
            cache = cdir / f"{day}_{int(st.st_mtime)}_{st.st_size}.pkl"
        if cache is not None and cache.exists():
            m1, order_usd = pd.read_pickle(cache)
        else:
            t = read_tick_file(f)
            orders, _gid = aggressor_orders(t)
            order_usd = orders["usd"].to_numpy(float) if len(t) else np.array([])
            m1 = ticks_to_flow_1m(t, thr)
            if cache is not None:
                for stale in cache.parent.glob(f"{day}_*.pkl"):
                    stale.unlink(missing_ok=True)   # one cache file per day (open day changes)
                pd.to_pickle((m1, order_usd), cache)
        if len(order_usd):
            hist.append(order_usd)
        parts.append(m1)
    if not parts:
        return pd.DataFrame(columns=FLOW_1M_COLS)
    out = pd.concat(parts)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    if start is not None:
        out = out[out.index >= int(start)]
    if end is not None:
        out = out[out.index < int(end)]
    return out


def continuous_1m(m1: pd.DataFrame, start: int, end: int) -> pd.DataFrame:
    """Reindex to every minute in [start, end): flow 0, price carried forward."""
    idx = np.arange(int(start) // 60 * 60, int(end), 60, dtype=np.int64)
    d = m1.reindex(idx)
    d["close"] = d["close"].ffill()
    for c in ("open", "high", "low"):
        d[c] = d[c].fillna(d["close"])
    for c in FLOW_1M_COLS:
        if c not in ("open", "high", "low", "close"):
            d[c] = d[c].fillna(0.0)
    d.index.name = "minute"
    return d


def resample_bars(c1: pd.DataFrame, tf_minutes: int) -> pd.DataFrame:
    """OHLCV + flow bars on a ``tf`` grid aligned to epoch (``time`` = bar open)."""
    key = (c1.index.to_numpy(np.int64) // (tf_minutes * 60)) * (tf_minutes * 60)
    g = c1.groupby(key, sort=True)
    out = pd.DataFrame({
        "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
        "close": g["close"].last(), "volume": g["volume"].sum(), "buy_vol": g["buy_vol"].sum(),
        "sell_vol": g["sell_vol"].sum(), "usd": g["usd"].sum(), "buy_usd": g["buy_usd"].sum(),
        "sell_usd": g["sell_usd"].sum(), "lbuy_usd": g["lbuy_usd"].sum(),
        "lsell_usd": g["lsell_usd"].sum(), "n_min": g["close"].size(),
    })
    out = out[out["n_min"] == tf_minutes].drop(columns="n_min")
    out.index.name = "time"
    return out.reset_index()


# ── features ───────────────────────────────────────────────────

def _z(x: pd.Series) -> pd.Series:
    sd = x.rolling(Z_WINDOW_MIN, min_periods=Z_MIN_PERIODS).std()
    return x / sd.replace(0, np.nan)


def minute_features(c1: pd.DataFrame) -> pd.DataFrame:
    """Trailing-window flow features on the continuous 1m grid (value at minute close)."""
    f = pd.DataFrame(index=c1.index)
    close = c1["close"].astype(float)
    net = c1["buy_usd"] - c1["sell_usd"]
    tot = c1["usd"]
    lnet = c1["lbuy_usd"] - c1["lsell_usd"]
    lr = np.log(close)
    for w in WINDOWS:
        nw = net.rolling(w, min_periods=w).sum()
        tw = tot.rolling(w, min_periods=w).sum()
        f[f"imb_{w}"] = nw / tw.replace(0, np.nan)
        f[f"net_z_{w}"] = _z(nw)
        f[f"lnet_z_{w}"] = _z(lnet.rolling(w, min_periods=w).sum())
        rw = lr - lr.shift(w)
        f[f"ret_{w}"] = rw
        f[f"ret_z_{w}"] = _z(rw)
        f[f"bull_abs_{w}"] = (f[f"net_z_{w}"] <= -1.0) & (f[f"ret_z_{w}"] >= 0.0)
        f[f"bear_abs_{w}"] = (f[f"net_z_{w}"] >= 1.0) & (f[f"ret_z_{w}"] <= 0.0)
    pq = (c1["usd"]).rolling(1440, min_periods=720).sum()
    q = c1["volume"].rolling(1440, min_periods=720).sum()
    vwap = pq / q.replace(0, np.nan)
    f["vwap24"] = vwap
    f["vwap_dev"] = (close - vwap) / close
    f["vwap_dev_z"] = _z(f["vwap_dev"])
    f["euphoria"] = (f["vwap_dev_z"] >= 2.5) & (f["lnet_z_60"] < 0)
    return f


def fine_features(c1: pd.DataFrame, mf: pd.DataFrame, fine_minutes: int = FINE_MIN) -> pd.DataFrame:
    """15m execution grid: OHLC + climax flags + the 1m features sampled at bar close."""
    bars = resample_bars(c1, fine_minutes)
    close_min = bars["time"].to_numpy(np.int64) + (fine_minutes - 1) * 60
    sampled = mf.reindex(close_min)
    sampled.index = bars.index
    fb = pd.concat([bars, sampled.reset_index(drop=True)], axis=1)
    rng = (fb["high"] - fb["low"]) / fb["close"]
    look = int(4 * 1440 / fine_minutes)
    vol = fb["usd"]
    fb["vol_z"] = (vol - vol.rolling(look, min_periods=look // 4).mean().shift(1)) / \
        vol.rolling(look, min_periods=look // 4).std().shift(1).replace(0, np.nan)
    fb["range_x"] = rng / rng.rolling(look, min_periods=look // 4).median().shift(1).replace(0, np.nan)
    n4h = int(240 / fine_minutes)
    hi4 = fb["high"].rolling(n4h, min_periods=n4h).max()
    lo4 = fb["low"].rolling(n4h, min_periods=n4h).min()
    pos = (fb["close"] - fb["low"]) / (fb["high"] - fb["low"]).replace(0, np.nan)
    spike = (fb["vol_z"] >= 3.0) & (fb["range_x"] >= 2.0)
    fb["buy_climax"] = spike & (fb["high"] >= hi4) & (pos <= 0.5)
    fb["sell_climax"] = spike & (fb["low"] <= lo4) & (pos >= 0.5)
    fb["sell_climax_recent"] = fb["sell_climax"].rolling(4, min_periods=1).max().astype(bool)
    fb["buy_climax_recent"] = fb["buy_climax"].rolling(4, min_periods=1).max().astype(bool)
    # dip absorbed: heavy selling over the last hour but the last 15 minutes held / bounced,
    # or a selling climax in the last hour.
    fb["dip_absorbed"] = ((fb["net_z_60"] <= -1.0) & (fb["ret_15"] >= 0)) | fb["sell_climax_recent"]
    for c in ("bull_abs_60", "bear_abs_60", "bull_abs_240", "bear_abs_240", "euphoria",
              "buy_climax", "sell_climax", "dip_absorbed"):
        fb[c] = fb[c].fillna(False).astype(bool)
    return fb


def tf_features(c1: pd.DataFrame, mf: pd.DataFrame, fb: pd.DataFrame, tf_minutes: int) -> pd.DataFrame:
    """Flow features for each ``tf`` bar, as known at its close (entry-filter inputs)."""
    bars = resample_bars(c1, tf_minutes)
    close_min = bars["time"].to_numpy(np.int64) + (tf_minutes - 1) * 60
    sampled = mf.reindex(close_min).reset_index(drop=True)
    out = pd.concat([bars, sampled], axis=1)
    # sell climax anywhere in the tf bar's trailing 4h of 15m bars
    fine_close = fb["time"].to_numpy(np.int64) + (FINE_MIN - 1) * 60
    sc = pd.Series(fb["sell_climax"].to_numpy(bool), index=fine_close)
    sc4h = sc.rolling(int(240 / FINE_MIN), min_periods=1).max().reindex(close_min)
    out["sell_climax_4h"] = sc4h.fillna(0).astype(bool).to_numpy()
    return out


# ── entry filters (True = entry allowed) ───────────────────────

def _b(d: pd.DataFrame, col: str) -> np.ndarray:
    if col not in d:
        return np.zeros(len(d), dtype=bool)
    return d[col].fillna(False).to_numpy(bool)


def _lt(d: pd.DataFrame, col: str, x: float) -> np.ndarray:
    if col not in d:
        return np.zeros(len(d), dtype=bool)
    with np.errstate(invalid="ignore"):
        return d[col].to_numpy(float) < x


ENTRY_FILTERS = {
    # skip entries while LARGE orders are net selling hard over the last 4h (distribution)
    "of_no_dist": lambda d: ~_lt(d, "lnet_z_240", -1.0) & np.isfinite(d.get("lnet_z_240", pd.Series(np.nan, index=d.index)).to_numpy(float)),
    # skip entries when 4h buying is being absorbed (price not rising on heavy buys)
    "of_no_bear_abs": lambda d: ~_b(d, "bear_abs_240") & np.isfinite(d.get("net_z_240", pd.Series(np.nan, index=d.index)).to_numpy(float)),
    # only enter after sellers were absorbed (heavy selling, price held) or a selling climax
    "of_abs_pref": lambda d: _b(d, "bull_abs_240") | _b(d, "sell_climax_4h"),
    # skip chasing: price already far above 24h VWAP
    "of_not_extended": lambda d: _lt(d, "vwap_dev_z", 2.5),
}


def entry_filter_mask(d: pd.DataFrame, name: str | None) -> np.ndarray:
    """Fail closed: missing flow columns block the entry when a filter is on."""
    if not name:
        return np.ones(len(d), dtype=bool)
    if name not in ENTRY_FILTERS:
        raise ValueError(f"unknown order-flow filter {name!r}; known: {sorted(ENTRY_FILTERS)}")
    with np.errstate(invalid="ignore"):
        return np.asarray(ENTRY_FILTERS[name](d), dtype=bool)


def flow_frames(tick_dir: Path | str, tf_minutes: int, *, start: int, end: int,
                cache_dir: Path | str | None = None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """(continuous 1m, 15m fine frame, tf flow frame) for [start, end)."""
    m1 = build_flow_1m(tick_dir, start=start - 8 * 86400, end=end, cache_dir=cache_dir)
    m1 = m1[m1.index >= start]
    c1 = continuous_1m(m1, start, end)
    mf = minute_features(c1)
    fb = fine_features(c1, mf)
    tfd = tf_features(c1, mf, fb, tf_minutes)
    return c1, fb, tfd

"""Signal builders for the perp research study (closed-bar, no look-ahead).

Each builder returns dict(long_entry, short_entry, long_exit, short_exit[, max_hold]).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .backtest_core import adx_hysteresis
from .technicals import ema


def _d1(d):
    on = d["d1_riskon"].to_numpy(float)
    known = np.isfinite(on)
    bull = known & (np.nan_to_num(on) > 0.5)
    bear_repo = known & ~bull                      # repo futures_short rule: known and not risk-on
    strict = np.nan_to_num(d["d1_bear_strict"].to_numpy(float)) > 0.5
    return bull, bear_repo, strict


def trend_ls(d: pd.DataFrame, p: dict) -> dict:
    """Trend-hold long when D1 risk-on, bearish mirror short when D1 not risk-on."""
    slow = int(p.get("ema_slow", 100))
    ef = d["ema20"].to_numpy(float)
    es = ema(d["close"], slow).to_numpy(float)
    c = d["close"].to_numpy(float)
    warm = np.arange(len(d)) >= slow
    bull, bear, _ = _d1(d)
    sides = p.get("sides", "both")
    le = warm & (c > es) & (ef > es) & bull
    se = warm & (c < es) & (ef < es) & bear
    if sides == "long":
        se = np.zeros(len(d), bool)
    if sides == "short":
        le = np.zeros(len(d), bool)
    return {"long_entry": le, "short_entry": se, "long_exit": c < es, "short_exit": c > es}


def regime_ls(d: pd.DataFrame, p: dict) -> dict:
    """Regime-switch (repo ``regime_signals``) long + its mirror short. Exits: chandelier
    (22-bar HH - k*ATR for longs, LL + k*ATR for shorts) or ADX chop. Stop/TP are not
    used (the perp sim exits only on signals or liquidation)."""
    adx_on = adx_hysteresis(d["adx"], 25.0, 20.0).to_numpy()
    lb = int(p.get("lookback", 20))
    k = float(p.get("atr_mult", 3.0))
    c = d["close"].to_numpy(float)
    atr = d["atr"].to_numpy(float)
    ph = d["high"].shift(1).rolling(lb).max().to_numpy(float)
    pl = d["low"].shift(1).rolling(lb).min().to_numpy(float)
    hh = d["high"].rolling(22, min_periods=1).max().to_numpy(float)
    ll = d["low"].rolling(22, min_periods=1).min().to_numpy(float)
    e20, e50, e200 = (d[x].to_numpy(float) for x in ("ema20", "ema50", "ema200"))
    bull, bear, _ = _d1(d)
    fin = np.isfinite(atr)
    le = adx_on & fin & (c > e200) & (e20 > e50) & (c > np.nan_to_num(ph, nan=np.inf)) & bull
    se = adx_on & fin & (c < e200) & (e20 < e50) & (c < np.nan_to_num(pl, nan=-np.inf)) & bear
    return {"long_entry": le, "short_entry": se,
            "long_exit": (c < hh - k * atr) | ~adx_on, "short_exit": (c > ll + k * atr) | ~adx_on}


def d1_flip(d: pd.DataFrame, p: dict) -> dict:
    """Daily-filter position: long while risk-on; short while bear (strict mirror or
    repo 'not risk-on'); flat otherwise. Acts only on the first bar after a daily close."""
    bull, bear_repo, strict = _d1(d)
    bear = strict if p.get("bear", "strict") == "strict" else bear_repo
    sides = p.get("sides", "both")
    if sides == "long":
        bear = np.zeros(len(d), bool)
    return {"long_entry": bull, "short_entry": bear, "long_exit": ~bull, "short_exit": ~bear}


def funding_fade(d: pd.DataFrame, p: dict, *, bar_hours: float) -> dict:
    """Fade crowded funding. f24 = mean hourly rate over the last 24 h that are known at
    the bar close (rates are charged to the bar they cover, so a bar's own rate is only
    known at its close). Rank f24 against the trailing 30 days. Short the top tail,
    long the bottom tail, hold a fixed number of hours."""
    r = d["rate"].to_numpy(float)
    per_bar = d["rate_n"].to_numpy(float) if "rate_n" in d else np.ones(len(d))
    hourly = pd.Series(r / np.maximum(per_bar, 1))  # mean hourly rate in the bar
    nb24 = max(int(round(24 / bar_hours)), 1)
    nb30 = int(round(720 / bar_hours))
    f24 = hourly.rolling(nb24, min_periods=nb24).mean()
    rank = f24.rolling(nb30, min_periods=nb30).rank(pct=True).to_numpy(float)
    q = float(p["q"])
    le = np.nan_to_num(rank, nan=0.5) <= (1 - q)
    se = np.nan_to_num(rank, nan=0.5) >= q
    if p.get("abs_floor"):
        # also require the raw level to be on the crowded side of zero
        f = f24.to_numpy(float)
        le &= np.nan_to_num(f) < 0
        se &= np.nan_to_num(f) > 0
    n = len(d)
    return {"long_entry": le, "short_entry": se, "long_exit": np.zeros(n, bool),
            "short_exit": np.zeros(n, bool), "max_hold": int(round(p["hold_h"] / bar_hours))}


def donchian_ls(d: pd.DataFrame, p: dict, *, bar_hours: float) -> dict:
    """Turtle-style channel breakout both ways (N-day high/low entry, M-day exit),
    optional volatility filter (ATR% rank >= 0.5). Research only: the repo refuses
    'breakout' for spot entries."""
    bpd = int(round(24 / bar_hours))
    N, M = int(p["entry_days"]) * bpd, int(p["exit_days"]) * bpd
    c = d["close"].to_numpy(float)
    hiN = d["high"].shift(1).rolling(N).max().to_numpy(float)
    loN = d["low"].shift(1).rolling(N).min().to_numpy(float)
    loM = d["low"].shift(1).rolling(M).min().to_numpy(float)
    hiM = d["high"].shift(1).rolling(M).max().to_numpy(float)
    vol_ok = np.ones(len(d), bool)
    if p.get("vol_filter"):
        vol_ok = np.nan_to_num(d["atr_rank"].to_numpy(float), nan=-1) >= 0.5
    le = vol_ok & (c > np.nan_to_num(hiN, nan=np.inf))
    se = vol_ok & (c < np.nan_to_num(loN, nan=-np.inf))
    return {"long_entry": le, "short_entry": se,
            "long_exit": c < np.nan_to_num(loM, nan=-np.inf), "short_exit": c > np.nan_to_num(hiM, nan=np.inf)}

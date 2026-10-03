"""Walk-forward search over a small library of 1h+ entry rules.

Promotion uses the existing paper bar (``promotion.check_strategy``): at least
30 out-of-sample trades, mean net bps > 0, 90% lower bound > 0, still positive
without the best two trades, and total P&L >= 0, all after fees.

A winner can be registered as a shadow sleeve. Shadow mode appends signals to
the shadow log and never books a paper fill and never touches a safety lock.

Momentum and any timeframe under 60 minutes are not in the library. Those
entries were refused after earlier walk-forwards lost to fees.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from .backtest_core import Costs, Spec, Trade, metrics, walk_forward
from .promotion import check_strategy

MIN_TF_MINUTES = 60
REFUSED_FAMILIES = frozenset({"momentum", "breakout"})
SHADOW_PATH = Path("logs/shadow_sleeves.json")


def library() -> dict[str, list[Spec]]:
    """Default paper-legal candidates. First spec of each family is the live default."""
    regime = [
        Spec("regime", {"lookback": lb, "atr_mult": am, "min_atr_rank": 0.0, "stop": st,
                        "tp": 0.25, "adx_enter": 25.0, "adx_exit": 20.0, "d1": True})
        for lb, am, st in ((20, 3.0, 0.03), (55, 3.0, 0.03), (20, 2.0, 0.05))
    ]
    meanrev = [
        Spec("meanrev", {"rsi_os": os_, "rsi_exit": 55.0, "stop": 0.03, "tp": 0.25,
                         "entry": "limit", "limit_offset": 0.001, "d1": True})
        for os_ in (38.0, 30.0)
    ]
    trend = [
        Spec("trendhold", {"ema_fast": 20, "ema_slow": slow, "d1": True})
        for slow in (100, 80)
    ]
    return {"regime": regime, "meanrev": meanrev, "trendhold": trend}


def _rows(trades: list[Trade], *, equity: float = 500.0) -> list[dict]:
    rows = []
    for t in trades:
        notional = min(equity * 0.01 / max(t.stop_frac, 1e-6), equity * 0.40)
        rows.append({"net_bps": t.net * 1e4, "pnl": notional * t.net})
    return rows


def buy_and_hold(df: pd.DataFrame, costs: Costs, *, warm: int = 210) -> dict | None:
    """One round trip, long the close path, after the same taker costs."""
    if df is None or len(df) < warm + 2:
        return None
    entry = float(df["open"].iloc[warm])
    exit_px = float(df["close"].iloc[-1])
    if entry <= 0 or exit_px <= 0:
        return None
    net = costs.net_return(entry, exit_px)
    close = df["close"].iloc[warm:].astype(float)
    peak = close.cummax()
    dd = float(((peak - close) / peak).max() * 100)
    return {"net_bps": net * 1e4, "return_pct": net * 100, "max_dd_pct": dd,
            "entry": entry, "exit": exit_px}


def evaluate_family(frames: list[pd.DataFrame], specs: list[Spec], costs: Costs, *,
                    folds: int = 4, equity: float = 500.0) -> dict:
    """Pool out-of-sample trades across symbols. ``frames`` are indicator-enriched."""
    oos: list[Trade] = []
    chosen: list[str] = []
    per_symbol = []
    for df in frames:
        if len(df) < 400:
            per_symbol.append({"bars": len(df), "skipped": True})
            continue
        wf = walk_forward(df, specs, costs, folds=folds)
        oos.extend(wf.oos_trades)
        chosen.extend(wf.chosen)
        m = metrics(wf.oos_trades, equity=equity)
        per_symbol.append({"bars": len(df), "skipped": False, **m.to_dict()})
    pooled = metrics(oos, equity=equity)
    bar = check_strategy(_rows(oos, equity=equity))
    holds = [buy_and_hold(df, costs) for df in frames]
    holds = [h for h in holds if h]
    bh = None
    if holds:
        bh = {
            "mean_net_bps": float(np.mean([h["net_bps"] for h in holds])),
            "mean_return_pct": float(np.mean([h["return_pct"] for h in holds])),
            "mean_max_dd_pct": float(np.mean([h["max_dd_pct"] for h in holds])),
        }
    return {
        "family": specs[0].name if specs else "",
        "oos": pooled.to_dict(),
        "promotion": bar,
        "passes": bool(bar["passes"]),
        "chosen": chosen,
        "per_symbol": per_symbol,
        "buy_and_hold": bh,
    }


def search(frames_by_tf: dict[int, list[pd.DataFrame]], costs: Costs, *,
           folds: int = 4, equity: float = 500.0) -> dict:
    """Run the library on each timeframe >= 60 minutes."""
    results = []
    for tf, frames in sorted(frames_by_tf.items()):
        if int(tf) < MIN_TF_MINUTES:
            results.append({"timeframe_minutes": int(tf), "refused": "entries under 1h are not searched"})
            continue
        for family, specs in library().items():
            if family in REFUSED_FAMILIES or specs[0].name in REFUSED_FAMILIES:
                continue
            row = evaluate_family(frames, specs, costs, folds=folds, equity=equity)
            row["timeframe_minutes"] = int(tf)
            results.append(row)
    winners = [r for r in results if r.get("passes")]
    return {"results": results, "winners": winners,
            "refused_families": sorted(REFUSED_FAMILIES), "min_tf_minutes": MIN_TF_MINUTES}


def register_shadow(winner: dict, *, path: Path | str = SHADOW_PATH, report: str = "") -> dict:
    """Append a shadow sleeve. Does not read or write settings, env, or locks."""
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            data = []
    except (OSError, ValueError):
        data = []
    rec = {
        "id": f"{winner.get('family')}@{winner.get('timeframe_minutes')}m|{time.strftime('%Y-%m-%d')}",
        "strategy": winner.get("family"),
        "timeframe_minutes": winner.get("timeframe_minutes"),
        "mode": "shadow",
        "fills": False,
        "registered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_report": report,
        "oos": winner.get("oos"),
    }
    if any(x.get("id") == rec["id"] for x in data):
        return rec
    data.append(rec)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)
    return rec


def load_shadow_sleeves(path: Path | str = SHADOW_PATH) -> list[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [r for r in data if isinstance(r, dict) and r.get("mode") == "shadow" and not r.get("fills")]


def shadow_signal(spec_name: str, params: dict, df: pd.DataFrame) -> bool:
    """True when the last closed bar would enter. No fill, no size."""
    from .backtest_core import meanrev_signals, regime_signals, trendhold_signals
    if df is None or len(df) < 30:
        return False
    if spec_name == "regime":
        sig = regime_signals(df, params)
        return bool(sig["entry"][-1])
    if spec_name == "meanrev":
        sig = meanrev_signals(df, params)
        return bool(sig["entry"][-1])
    if spec_name == "trendhold":
        sig = trendhold_signals(df, params)
        return bool(sig["entry"][-1])
    return False


def shadow_cycle(settings, *, fetch_bars=None, shadow_path: Path | str = SHADOW_PATH) -> list[dict]:
    """Log shadow-sleeve entries. Never books a fill and never reads locks.

    No registered sleeves → no market-data calls.
    """
    sleeves = load_shadow_sleeves(shadow_path)
    if not sleeves:
        return []
    from .backtest_core import add_indicators
    from .shadow import ShadowLog
    log = ShadowLog()
    written = []
    for rec in sleeves:
        if rec.get("fills"):
            continue
        tf = int(rec.get("timeframe_minutes") or 60)
        if tf < MIN_TF_MINUTES:
            continue
        name = str(rec.get("strategy") or "")
        if name in REFUSED_FAMILIES:
            continue
        params = library().get(name, [None])[0]
        if params is None:
            continue
        for symbol in ("BTC/USD", "ETH/USD", "SOL/USD"):
            try:
                raw = fetch_bars(symbol, tf) if fetch_bars else None
                if raw is None:
                    from .shadow import public_bars
                    raw = public_bars(symbol, tf)
                df = add_indicators(raw)
            except Exception:
                continue
            if not shadow_signal(name, params.params, df):
                continue
            if log.record(strategy=f"shadow:{name}@{tf}m", symbol=symbol, gate="shadow_research",
                          reason="shadow sleeve entry (no fill)", signal_bar_open=float(df["time"].iloc[-1])
                          if "time" in df else 0.0,
                          tf_minutes=tf, signal_px=float(df["close"].iloc[-1]), params=params.params):
                written.append({"strategy": name, "symbol": symbol, "tf": tf})
    return written


def improves(base_bps: float, alt_bps: float, base_ret: float, alt_ret: float,
             base_dd: float, alt_dd: float, alt_trades: int, *, min_trades: int = 8) -> bool:
    """Pre-registered rule for turning a trailing-stop overlay on.

    Out-of-sample mean bps and total return must both rise, drawdown must not
    worsen by more than 1 percentage point, and the overlay needs at least
    ``min_trades`` OOS trades. Otherwise the sleeve stays off.
    """
    if alt_trades < min_trades:
        return False
    return (alt_bps > base_bps and alt_ret > base_ret and alt_dd <= base_dd + 1.0)

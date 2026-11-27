"""Structured paper telemetry: decisions, equity, git hash, excursions.

Append-only JSONL files under ``logs/`` (never committed, never contain secrets):

* ``decision_snapshots.jsonl``  one row per (strategy, symbol, closed bar, action) decision:
  bar time, indicator snapshot, daily-filter (D1) state, bid/ask/spread, data age,
  strategy id, learner state, git hash.
* ``equity.jsonl``     one row per loop cycle: cash, open positions at market,
  equity, exposure, per-sleeve owners.
* ``closed_trades.jsonl`` rich rows are written by ``LearningAgent.record_trade``
  (see learner.py); the scorecard keeps it deduplicated.
"""
from __future__ import annotations
from .timeutil import epoch_seconds

import json
import math
import subprocess
import time
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
_GIT: str | None = None
INDICATORS = ("close", "ema20", "ema50", "ema200", "adx", "atr", "atr_rank", "rsi",
              "d1_riskon", "d1_sma50")


def git_hash() -> str:
    """Short git hash of the running code (cached; 'unknown' outside a checkout)."""
    global _GIT
    if _GIT is None:
        try:
            out = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                                 capture_output=True, text=True, timeout=5)
            _GIT = out.stdout.strip() or "unknown"
        except Exception:
            _GIT = "unknown"
    return _GIT


def _num(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, 8) if math.isfinite(v) else None


def append_jsonl(path: Path | str, row: dict) -> None:
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str, sort_keys=True) + "\n")
    except OSError:
        pass


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))


def indicator_snapshot(frame: pd.DataFrame | None, extra: dict | None = None) -> dict:
    """Last-row indicator values (whatever the frame has) + strategy extras."""
    out: dict[str, Any] = {}
    if frame is not None and len(frame):
        last = frame.iloc[-1]
        for k in INDICATORS:
            if k in frame:
                out[k] = _num(last[k])
    for k, v in (extra or {}).items():
        out[k] = _num(v)
    return out


def bar_open_ts(bars: pd.DataFrame | None) -> float | None:
    if bars is None or not len(bars):
        return None
    if "time" in bars:
        return _num(bars["time"].iloc[-1])
    idx = bars.index[-1]
    return float(idx.timestamp()) if hasattr(idx, "timestamp") else None


def quote_fields(t: dict | None) -> dict:
    if not t:
        return {"bid": None, "ask": None, "spread_bps": None}
    bid, ask = _num(t.get("bid")), _num(t.get("ask"))
    spread = None
    if bid and ask and ask > 0:
        spread = round((ask - bid) / ((ask + bid) / 2) * 1e4, 2)
    return {"bid": bid, "ask": ask, "spread_bps": spread}


def excursion(bars: pd.DataFrame | None, entry_ts: float | None, entry_px: float) -> tuple[float | None, float | None]:
    """(MAE bps, MFE bps) of a long from ``entry_ts`` over the bars seen so far."""
    if bars is None or not len(bars) or not entry_ts or not entry_px:
        return None, None
    try:
        if "time" in bars:
            opens = pd.to_numeric(bars["time"], errors="coerce").to_numpy(float)
        else:
            opens = epoch_seconds(bars.index).astype(float)
        # bars that overlap the holding period (the entry bar included)
        tf = float(opens[-1] - opens[-2]) if len(opens) > 1 else 0.0
        m = opens + tf > float(entry_ts)
        if not m.any():
            return None, None
        lo = float(bars["low"].to_numpy(float)[m].min())
        hi = float(bars["high"].to_numpy(float)[m].max())
        return round((lo / entry_px - 1) * 1e4, 1), round((hi / entry_px - 1) * 1e4, 1)
    except Exception:
        return None, None


class DecisionLog:
    """``logs/decision_snapshots.jsonl`` — one row per new (strategy, symbol, bar, action).

    (``logs/decisions.jsonl`` is the engine's older DecisionRecord journal.)"""

    def __init__(self, path: Path | str = Path("logs/decision_snapshots.jsonl")) -> None:
        self.path = Path(path)
        self._last: dict[str, tuple] = {}

    def is_new(self, strategy: str, symbol: str, bar_open: float | None, action: str) -> bool:
        return self._last.get(f"{strategy}|{symbol}") != (bar_open, action)

    def log(self, *, strategy: str, symbol: str, action: str, reason: str,
            bar_open: float | None, tf_minutes: int, indicators: dict | None = None,
            d1: dict | None = None, quote: dict | None = None, learner: dict | None = None,
            now: float | None = None, force: bool = False, **extra) -> bool:
        key = f"{strategy}|{symbol}"
        sig = (bar_open, action)
        if not force and self._last.get(key) == sig:
            return False
        self._last[key] = sig
        now = time.time() if now is None else float(now)
        bar_close = None if bar_open is None else float(bar_open) + tf_minutes * 60
        row = {"ts": iso(now), "strategy": strategy, "symbol": symbol, "action": action,
               "reason": (reason or "")[:300], "bar_open": iso(bar_open), "bar_close": iso(bar_close),
               "data_age_s": None if bar_close is None else round(now - bar_close, 1),
               "indicators": indicators or {}, "d1": d1, **quote_fields(quote),
               "learner": learner, "git": git_hash(), **extra}
        append_jsonl(self.path, row)
        return True


DECISIONS = DecisionLog()


def d1_state(frame: pd.DataFrame | None, enabled: bool) -> dict:
    if not enabled:
        return {"enabled": False}
    if frame is None or "d1_riskon" not in frame or not len(frame):
        return {"enabled": True, "riskon": None, "note": "not evaluated (in position or no data)"}
    v = _num(frame["d1_riskon"].iloc[-1])
    return {"enabled": True, "riskon": None if v is None else bool(v > 0.5),
            "sma50_d": _num(frame["d1_sma50"].iloc[-1]) if "d1_sma50" in frame else None}


def positions_of(data: dict) -> dict[str, dict]:
    """PaperPortfolio JSON stores positions as a list of {symbol, ...}; accept a dict too."""
    raw = data.get("positions") if isinstance(data, dict) else None
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}
    if isinstance(raw, list):
        return {str(p["symbol"]): p for p in raw if isinstance(p, dict) and p.get("symbol")}
    return {}


def equity_snapshot(portfolio_path: Path | str, prices: dict[str, float], *, seed: float,
                    owners: dict | None = None, now: float | None = None) -> dict:
    """Mark the paper book to market (public prices only)."""
    try:
        data = json.loads(Path(portfolio_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    cash = float(data.get("cash", seed) or 0.0)
    pos_out, mtm, basis = {}, 0.0, 0.0
    for sym, p in positions_of(data).items():
        q = float(p.get("quantity", 0) or 0)
        if q <= 1e-12:
            continue
        px = prices.get(sym)
        ep = float(p.get("entry_price", 0) or 0)
        val = q * (px if px else ep)
        mtm += val
        basis += q * ep
        pos_out[sym] = {"qty": q, "entry": ep, "mark": px, "value": round(val, 4),
                        "owner": (owners or {}).get(sym)}
    eq = cash + mtm
    return {"ts": iso(time.time() if now is None else now), "cash": round(cash, 4),
            "positions_value": round(mtm, 4), "equity": round(eq, 4),
            "cost_basis_open": round(basis, 4), "exposure_pct": round(mtm / eq * 100, 2) if eq > 0 else None,
            "open_positions": len(pos_out), "positions": pos_out, "seed": seed, "git": git_hash()}


def trade_detail(entry: dict, *, exit_signal_px: float | None, exit_fill_px: float, exit_fee: float,
                 exit_maker: bool, reason: str, bars: pd.DataFrame | None, now: float,
                 qty: float | None = None, cost_basis: float | None = None) -> dict:
    """closed_trades.jsonl detail from an entry record (sleeve position / learner entry meta).

    ``entry`` keys used: signal_px, entry (fill px), entry_fee, entry_maker, filled_at, signal_bar.
    """
    ep = _num(entry.get("entry") or entry.get("entry_fill_px"))
    filled = _num(entry.get("filled_at"))
    mae, mfe = excursion(bars, filled, ep) if ep else (None, None)
    return {
        "entry_signal_px": _num(entry.get("signal_px")), "entry_fill_px": ep,
        "entry_fee": _num(entry.get("entry_fee")), "entry_maker": bool(entry.get("entry_maker", False)),
        "entry_time": iso(filled), "signal_bar": entry.get("signal_bar"),
        "exit_signal_px": _num(exit_signal_px), "exit_fill_px": _num(exit_fill_px),
        "exit_fee": _num(exit_fee), "exit_maker": bool(exit_maker), "exit_reason": str(reason)[:120],
        "mae_bps": mae, "mfe_bps": mfe,
        "hold_s": None if filled is None else round(float(now) - filled, 1),
        "qty": _num(qty), "cost_basis": _num(cost_basis),
    }


def _gate_name(reason: str) -> str:
    r = (reason or "").lower()
    for key, name in (("learner", "learner"), ("max concurrent", "max_positions"), ("exposure", "exposure"),
                      ("another sleeve", "ownership"), ("market quality", "market_quality"),
                      ("daily loss", "daily_loss"), ("drawdown", "drawdown"), ("cooldown", "cooldown"),
                      ("cash", "cash"), ("fee", "fee_edge"), ("risk", "risk"), ("d1", "d1")):
        if key in r:
            return name
    return "other"


def blocked_entry(sleeve, symbol: str, sig, bars: pd.DataFrame | None, reason: str,
                  gate: str | None = None) -> dict:
    """A shadow-trade candidate from a sleeve (see shadow.py)."""
    params = {}
    try:
        params = dict(sleeve.strategy.params())
    except Exception:
        pass
    return {"symbol": symbol, "gate": gate or _gate_name(reason), "reason": (reason or "")[:240],
            "signal_px": _num(getattr(sig, "price", None)), "signal_bar_open": bar_open_ts(bars),
            "tf_minutes": int(sleeve.tf_seconds // 60), "strategy": sleeve.strategy_key, "params": params}


def sleeve_decision(sleeve, symbol: str, sig, bars: pd.DataFrame | None, *, in_position: bool,
                    d1_enabled: bool) -> None:
    """Decision snapshot for a paper sleeve; never raises."""
    try:
        bo = bar_open_ts(bars)
        action = getattr(getattr(sig, "action", None), "value", str(getattr(sig, "action", "")))
        if not DECISIONS.is_new(sleeve.strategy_key, symbol, bo, action):
            return
        quote = None
        try:
            quote = sleeve.gateway.get_ticker_for(symbol)
        except Exception:
            pass
        frame = getattr(sleeve.strategy, "last_frame", None)
        DECISIONS.log(strategy=sleeve.strategy_key, symbol=symbol, action=action, reason=sig.reason,
                      bar_open=bo, tf_minutes=int(sleeve.tf_seconds // 60),
                      indicators=indicator_snapshot(frame), d1=d1_state(frame, d1_enabled and not in_position),
                      quote=quote, learner=sleeve.learner.snapshot(symbol), now=sleeve.now_fn(),
                      in_position=in_position, d1_blocked=bool(getattr(sleeve.strategy, "d1_blocked", False)))
    except Exception:
        pass

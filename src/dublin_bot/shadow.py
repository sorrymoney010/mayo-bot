"""Shadow trades: entry signals that a gate blocked, scored later.

Every BUY signal that a gate stops (learner bench, risk caps, max positions,
sleeve ownership, market quality, the daily D1 filter, ...) is appended to
``logs/shadow_signals.jsonl`` as a ``{"type": "signal", ...}`` row. Later cycles
score it: the strategy's own backtest rules (``backtest_core.simulate``) are
replayed from the signal bar on closed public OHLC bars and a
``{"type": "outcome", "id": ..., "net_bps": ...}`` row is appended once the
hypothetical trade has exited (or it is marked ``no_fill`` / ``not_reproduced``
/ ``expired``). The file is append-only; readers join on ``id``.

Paper telemetry only: this never places, sizes or blocks anything.
"""
from __future__ import annotations
from .timeutil import series_epoch_seconds

import json
import time
from pathlib import Path
from typing import Callable

import pandas as pd

from .backtest_core import Costs, Spec, add_indicators, simulate
from .telemetry import append_jsonl, git_hash, iso

SHADOW_PATH = Path("logs/shadow_signals.jsonl")
EXPIRE_DAYS = 45.0
CONTEXT_BARS = 300  # bars before the signal bar fed to simulate (indicator / hysteresis state)

_FAMILY = {"regime": "regime", "meanrev_mk": "meanrev", "meanrev": "meanrev", "trendhold": "trendhold"}


def spec_for(strategy_key: str, params: dict) -> Spec | None:
    fam = strategy_key.split("@", 1)[0]
    name = _FAMILY.get(fam)
    if name is None:
        return None
    p = {k: v for k, v in (params or {}).items() if k != "warm"}
    p["d1"] = False  # score the raw rules: the block being evaluated may BE the D1 filter
    if fam == "meanrev_mk":
        p["entry"] = "limit"
    return Spec(name, p)


class ShadowLog:
    def __init__(self, path: Path | str = SHADOW_PATH) -> None:
        self.path = Path(path)

    def rows(self) -> list[dict]:
        out: list[dict] = []
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        except OSError:
            pass
        return out

    def record(self, *, strategy: str, symbol: str, gate: str, reason: str, signal_bar_open: float,
               tf_minutes: int, signal_px: float, params: dict, now: float | None = None,
               extra: dict | None = None) -> bool:
        """Append a blocked-signal row (once per strategy/symbol/bar)."""
        sid = f"{strategy}|{symbol}|{int(signal_bar_open)}"
        if any(r.get("id") == sid for r in self.rows() if r.get("type") == "signal"):
            return False
        now = time.time() if now is None else float(now)
        append_jsonl(self.path, {
            "type": "signal", "id": sid, "ts": iso(now), "strategy": strategy, "symbol": symbol,
            "gate": gate, "reason": (reason or "")[:240], "tf_minutes": int(tf_minutes),
            "signal_bar_open": int(signal_bar_open), "signal_bar": iso(signal_bar_open),
            "signal_px": float(signal_px), "params": params, "git": git_hash(), **(extra or {})})
        return True

    def open_signals(self) -> list[dict]:
        rows = self.rows()
        done = {r.get("id") for r in rows if r.get("type") == "outcome"}
        return [r for r in rows if r.get("type") == "signal" and r.get("id") not in done]

    def score(self, fetch_bars: Callable[[str, int], pd.DataFrame], *, now: float | None = None,
              costs: Costs | None = None) -> list[dict]:
        """Score open shadow signals whose hypothetical trade has finished."""
        now = time.time() if now is None else float(now)
        costs = costs or Costs()
        cache: dict[tuple[str, int], pd.DataFrame | None] = {}
        written: list[dict] = []
        for r in self.open_signals():
            tf = int(r.get("tf_minutes", 60))
            t0 = int(r["signal_bar_open"])
            if now < t0 + 2 * tf * 60:  # need at least the entry bar closed
                continue
            out = None
            if now - t0 > EXPIRE_DAYS * 86400:
                out = {"status": "expired"}
            else:
                spec = spec_for(str(r.get("strategy", "")), r.get("params") or {})
                if spec is None:
                    out = {"status": "unscorable", "note": "no backtest family for this strategy"}
                else:
                    key = (str(r["symbol"]), tf)
                    if key not in cache:
                        try:
                            cache[key] = fetch_bars(*key)
                        except Exception:
                            cache[key] = None
                    out = score_one(cache[key], spec, t0, tf, costs)
            if out is None:
                continue  # still open in the hypothetical world, or data not there yet
            row = {"type": "outcome", "id": r["id"], "ts": iso(now), "strategy": r.get("strategy"),
                   "symbol": r.get("symbol"), "gate": r.get("gate"), **out}
            append_jsonl(self.path, row)
            written.append(row)
        return written


def score_one(bars: pd.DataFrame | None, spec: Spec, t0: int, tf: int, costs: Costs) -> dict | None:
    """Replay ``spec`` from the signal bar (open time ``t0``). None = not decided yet."""
    if bars is None or not len(bars):
        return None
    d = bars.copy()
    if "time" not in d:
        d = d.reset_index(names="ts")
        d["time"] = series_epoch_seconds(d["ts"]).astype(int)
    d = d.sort_values("time").reset_index(drop=True)
    hits = d.index[d["time"].astype(int) == int(t0)]
    if not len(hits):
        return {"status": "not_reproduced", "note": "signal bar not in fetched OHLC"} \
            if int(d["time"].iloc[0]) > t0 else None
    i = int(hits[0])
    if i < 60:
        return {"status": "not_reproduced", "note": "not enough history before the signal bar"}
    k = min(i, CONTEXT_BARS)
    sub = add_indicators(d).iloc[i - k:].reset_index(drop=True)
    trades = simulate(sub, Spec(spec.name, {**spec.params, "warm": k}), costs)
    first = trades[0] if trades else None
    if first is None or first.entry_i != k + 1:
        if spec.params.get("entry") == "limit" and len(sub) > k + 1:
            return {"status": "no_fill", "net_bps": 0.0, "note": "limit would not have filled"}
        if len(sub) > k + 1:
            return {"status": "not_reproduced", "note": "rules did not re-fire on public OHLC"}
        return None
    if first.reason == "eod_mark":
        return None  # hypothetical trade still open
    return {"status": "scored", "entry_px": round(first.entry_px, 8), "exit_px": round(first.exit_px, 8),
            "entry_time": iso(first.entry_time), "exit_time": iso(first.exit_time),
            "exit_reason": first.reason, "hold_h": round((first.exit_time - first.entry_time) / 3600, 2),
            "net_bps": round(first.net * 1e4, 1), "gross_bps": round(first.gross * 1e4, 1)}


def public_bars(symbol: str, tf_minutes: int, *, now: float | None = None) -> pd.DataFrame:
    """Closed Kraken public OHLC bars (time = bar open, epoch s). No private API."""
    from .daily_filter import _PAIR
    from .pipeline.kraken_rest import KrakenPublic

    now = time.time() if now is None else float(now)
    pair = _PAIR.get(symbol.upper(), symbol.replace("/", ""))
    rows = KrakenPublic(min_interval=0.0, timeout=10.0).ohlc(pair, int(tf_minutes), retries=2)
    df = pd.DataFrame([r[:7] for r in rows], columns=["time", "open", "high", "low", "close", "vwap", "volume"])
    df = df.drop(columns=["vwap"]).astype({"time": int, "open": float, "high": float, "low": float,
                                           "close": float, "volume": float})
    return df[df["time"] + tf_minutes * 60 <= now].reset_index(drop=True)

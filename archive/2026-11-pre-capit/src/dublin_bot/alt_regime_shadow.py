"""SHADOW alt-coin regime sleeve: regime_trend@240m on 8 alts, virtual ledger only.

Candidate from research/SIGNAL_FREQUENCY_REPORT.md (ADX 25/20, 20-bar breakout,
chandelier 3xATR, 3% stop, D1 filter OFF). It places NO orders, books nothing into
the paper portfolio or learner, reads no keys and touches no safety lock. Data is
Kraken PUBLIC spot OHLC only. It writes only:
  logs/alt_regime_shadow.jsonl        (status / entry / exit events)
  logs/alt_regime_shadow_state.json   (open virtual positions, last processed bar)
Caps: 1 position per coin, <= max_open positions, position_fraction of the virtual book.
Costs: 40 bps taker + 5 bps slippage per side.
"""
from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path
from typing import Callable

import pandas as pd

from .backtest_core import add_indicators, regime_signals

FEE_BPS, SLIP_BPS = 40.0, 5.0
SETTLE = 90
PAIR = {"DOGE/USD": "XDGUSD"}


def _public_ohlc(symbol: str, tf: int) -> pd.DataFrame:
    from .pipeline.kraken_rest import KrakenPublic
    pair = PAIR.get(symbol, symbol.replace("/", ""))
    rows = KrakenPublic(min_interval=0.3, timeout=8.0).ohlc(pair, tf, retries=1)
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "vwap", "volume", "count"])
    df = df.astype(float)
    df["time"] = df["time"].astype("int64")
    df.index = pd.to_datetime(df["time"], unit="s", utc=True)
    return df[["time", "open", "high", "low", "close", "volume"]]


class AltRegimeShadow:
    name = "alt_regime"

    def __init__(self, *, symbols, tf_minutes=240, logs_dir=Path("logs"), book_usd=500.0,
                 position_fraction=0.08, max_open=3, adx_enter=25.0, adx_exit=20.0,
                 lookback=20, atr_mult=3.0, stop=0.03,
                 fetch: Callable[[str, int], pd.DataFrame] | None = None) -> None:
        self.symbols = list(symbols)
        self.tf_minutes = int(tf_minutes)
        self.logs_dir = Path(logs_dir)
        self.book_usd = float(book_usd)
        self.frac = float(position_fraction)
        self.max_open = int(max_open)
        self.p = dict(adx_enter=adx_enter, adx_exit=adx_exit, lookback=lookback,
                      atr_mult=atr_mult, stop=stop, d1=False)
        self.fetch = fetch or _public_ohlc
        self.events = self.logs_dir / "alt_regime_shadow.jsonl"
        self.state_path = self.logs_dir / "alt_regime_shadow_state.json"
        self._last_try = 0.0

    # ── files ──
    def load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {"positions": {}, "last_bar": 0, "realized_usd": 0.0, "closed": 0}

    def _save(self, st: dict) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
        tmp.replace(self.state_path)

    def _emit(self, kind: str, **kw) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "sleeve": self.name,
               "kind": kind, "shadow": True, **kw}
        with self.events.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    # ── schedule ──
    def _bar_open(self, now: float) -> int:
        tf = self.tf_minutes * 60
        return int((now - SETTLE) // tf * tf) - tf  # last CLOSED bar's open

    def due(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if now - self._last_try < 240:
            return False
        return self._bar_open(now) > int(self.load().get("last_bar", 0))

    # ── cycle ──
    def run_cycle(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        self._last_try = now
        st = self.load()
        target = self._bar_open(now)
        pos = st.setdefault("positions", {})
        out = {"ran": True, "bar": target, "entries": [], "exits": [], "errors": []}
        cost = (FEE_BPS + SLIP_BPS) / 1e4
        for sym in self.symbols:
            try:
                df = self.fetch(sym, self.tf_minutes)
                df = df[df["time"] <= target]
                if len(df) < 260 or int(df["time"].iloc[-1]) != target:
                    out["errors"].append(f"{sym}: no closed bar {target}")
                    continue
                d = add_indicators(df)
                sig = regime_signals(d, self.p)
                i = len(d) - 1
                c, lo = float(d["close"].iloc[i]), float(d["low"].iloc[i])
                if sym in pos:
                    ps = pos[sym]
                    px, why = None, ""
                    if lo <= ps["stop"]:
                        px, why = ps["stop"], "stop"
                    elif sig["chandelier_exit"][i]:
                        px, why = c, "chandelier"
                    elif not sig["adx_on"][i]:
                        px, why = c, "regime_chop"
                    if px is not None:
                        net = (px * (1 - cost)) / (ps["entry"] * (1 + cost)) - 1
                        pnl = ps["notional"] * net
                        st["realized_usd"] = round(st.get("realized_usd", 0.0) + pnl, 4)
                        st["closed"] = st.get("closed", 0) + 1
                        del pos[sym]
                        self._emit("exit", symbol=sym, price=px, reason=why,
                                   net_bps=round(net * 1e4, 1), pnl_usd=round(pnl, 4))
                        out["exits"].append(sym)
                elif bool(sig["entry"][i]) and math.isfinite(c):
                    if len(pos) >= self.max_open:
                        self._emit("skip", symbol=sym, reason=f"max_open={self.max_open}")
                        continue
                    pos[sym] = {"entry": c, "stop": c * (1 - self.p["stop"]), "bar": target,
                                "notional": round(self.book_usd * self.frac, 4)}
                    self._emit("entry", symbol=sym, price=c, adx=round(float(d["adx"].iloc[i]), 1),
                               notional=pos[sym]["notional"])
                    out["entries"].append(sym)
            except Exception as exc:  # noqa: BLE001
                out["errors"].append(f"{sym}: {type(exc).__name__}: {exc}"[:200])
        st["last_bar"] = target
        self._save(st)
        self._emit("status", bar=target, open=sorted(pos), realized_usd=st.get("realized_usd", 0.0),
                   closed=st.get("closed", 0), errors=out["errors"][:8])
        return out


def build_alt_shadow(settings, **kw) -> AltRegimeShadow | None:
    if not (settings.paper_trading and settings.dry_run and not settings.allow_live_trading):
        return None
    if not getattr(settings, "alt_regime_shadow_enabled", False):
        return None
    g = lambda k, dflt: getattr(settings, k, dflt)  # noqa: E731
    return AltRegimeShadow(symbols=g("alt_regime_symbols", []),
                           tf_minutes=g("alt_regime_timeframe_minutes", 240),
                           position_fraction=g("alt_regime_position_fraction", 0.08),
                           max_open=g("alt_regime_max_open_positions", 3), **kw)


class AltShadowRunner:
    """Run the shadow in a daemon thread; never blocks or crashes the paper loop."""

    def __init__(self, shadow: AltRegimeShadow, log: Callable[[str], None]) -> None:
        self.s, self.log, self._t = shadow, log, None

    def _work(self) -> None:
        try:
            r = self.s.run_cycle()
            self.log(f"SHADOW_ALT bar={r['bar']} entries={r['entries']} exits={r['exits']} "
                     f"errors={len(r['errors'])}")
        except Exception as exc:  # noqa: BLE001
            self.log(f"WARN SHADOW_ALT {type(exc).__name__}: {exc}"[:400])

    def tick(self) -> None:
        try:
            if self._t is not None and self._t.is_alive():
                return
            if self.s.due():
                self._t = threading.Thread(target=self._work, name="shadow-alt", daemon=True)
                self._t.start()
        except Exception as exc:  # noqa: BLE001
            self.log(f"WARN SHADOW_ALT tick: {type(exc).__name__}: {exc}"[:400])

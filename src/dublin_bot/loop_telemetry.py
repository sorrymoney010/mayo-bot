"""Per-cycle telemetry for ``scripts/paper_trader_loop.py`` (paper only).

* decision snapshot of the primary engine  -> logs/decision_snapshots.jsonl
* blocked entry signals (all sleeves)      -> logs/shadow_signals.jsonl
* shadow outcome scoring (every 15 min)    -> logs/shadow_signals.jsonl (outcome rows)
* marked-to-market paper equity (5 min)    -> logs/equity.jsonl

Public market data only (active venue public REST); never touches private endpoints,
never places or blocks an order. Every method swallows its own errors.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from .shadow import ShadowLog, public_bars
from .telemetry import DECISIONS, append_jsonl, equity_snapshot, positions_of

EQUITY_EVERY = 300.0
SCORE_EVERY = 900.0
_BASE = {"BTC/USD": "XBT", "ETH/USD": "ETH", "SOL/USD": "SOL"}


def public_prices(symbols: list[str]) -> dict[str, float]:
    """Last prices from the active venue (Kraken or Binance.US public REST)."""
    from .venue import public_prices as _venue_prices

    return _venue_prices(symbols)


class LoopTelemetry:
    def __init__(self, settings, *, logs_dir: Path | str = Path("logs"), now_fn: Callable[[], float] = time.time,
                 price_fn: Callable[[list[str]], dict[str, float]] = public_prices,
                 bars_fn=public_bars, log: Callable[[str], None] = lambda m: None) -> None:
        self.settings = settings
        self.logs = Path(logs_dir)
        self.now_fn = now_fn
        self.price_fn = price_fn
        self.bars_fn = bars_fn
        self.log = log
        self.shadow = ShadowLog(self.logs / "shadow_signals.jsonl")
        self._last_equity = 0.0
        self._last_score = 0.0

    def _shadow(self, cands: list[dict]) -> int:
        n = 0
        for c in cands or []:
            try:
                if c.get("signal_bar_open") is None or c.get("signal_px") is None:
                    continue
                n += bool(self.shadow.record(
                    strategy=str(c["strategy"]), symbol=str(c["symbol"]), gate=str(c.get("gate", "other")),
                    reason=str(c.get("reason", "")), signal_bar_open=float(c["signal_bar_open"]),
                    tf_minutes=int(c.get("tf_minutes", 60)), signal_px=float(c["signal_px"]),
                    params=dict(c.get("params") or {}), now=self.now_fn()))
            except Exception:
                continue
        return n

    def engine(self, engine) -> None:
        try:
            ctx = dict(getattr(engine, "decision_ctx", None) or {})
            if ctx:
                quote = ctx.pop("quote", None)
                DECISIONS.log(quote=quote, now=self.now_fn(), **ctx)
            n = self._shadow(getattr(engine, "shadow_candidates", None) or [])
            if n:
                self.log(f"SHADOW recorded {n} blocked {ctx.get('strategy', 'primary')} signal(s)")
        except Exception:
            pass

    def sleeve(self, result: dict) -> None:
        try:
            n = self._shadow(result.get("blocked") or [])
            if n:
                self.log(f"SHADOW recorded {n} blocked sleeve={result.get('sleeve')} signal(s)")
        except Exception:
            pass

    def tick(self) -> None:
        now = float(self.now_fn())
        if now - self._last_equity >= EQUITY_EVERY:
            self._last_equity = now
            try:
                book = self.logs / "paper_portfolio.json"
                try:
                    held = list(positions_of(json.loads(book.read_text(encoding="utf-8"))).keys())
                except (OSError, ValueError):
                    held = []
                try:
                    prices = self.price_fn(held) if held else {}
                except Exception:
                    prices = {}
                try:
                    owners = json.loads((self.logs / "paper_sleeve_owners.json").read_text()).get("owners", {})
                except (OSError, ValueError):
                    owners = {}
                row = equity_snapshot(book, prices, seed=float(self.settings.strategy_equity_usd),
                                      owners=owners, now=now)
                row["marks"] = "public_ticker" if prices else "entry_price"
                append_jsonl(self.logs / "equity.jsonl", row)
            except Exception:
                pass
        if now - self._last_score >= SCORE_EVERY:
            self._last_score = now
            try:
                done = self.shadow.score(self.bars_fn, now=now)
                if done:
                    self.log("SHADOW scored " + ", ".join(
                        f"{r['strategy']}:{r['symbol']}:{r.get('status')}:{r.get('net_bps')}" for r in done)[:600])
            except Exception:
                pass

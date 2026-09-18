"""Self-learning agent — closes the loop that the reporting-only learning.py leaves open.

The bot records every *closed trade* (a sell that realizes P&L on a prior buy)
keyed by coin + market regime into a small persistent store. Selection then
biases toward coins with proven positive expectancy and away from bleeders,
exactly how we hand-tuned PUMP focus — but now learned automatically from the
bot's own results instead of a hardcoded constant.

This is deliberately conservative: a coin is only trusted enough to *bias*
selection after ``min_trades`` closed trades, so a single lucky or unlucky fill
can never hijack the bot. Below that threshold the learner is advisory only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CoinStats:
    symbol: str
    trades: int = 0
    wins: int = 0
    pnl: float = 0.0
    best_regime: str = "unknown"
    # Per-regime P&L, used to skip a coin in regimes where it bleeds.
    regime_pnl: dict[str, float] = field(default_factory=dict)

    @property
    def win_rate(self) -> float:
        return (self.wins / self.trades) if self.trades else 0.0

    @property
    def expectancy(self) -> float:
        """Average realized P&L per closed trade (the thing we actually want > 0)."""
        return (self.pnl / self.trades) if self.trades else 0.0


class LearningAgent:
    def __init__(self, path: str | Path, min_trades: int = 3, enabled: bool = True):
        self.path = Path(path)
        self.min_trades = min_trades
        self.enabled = enabled
        self.coins: dict[str, CoinStats] = {}
        self.last_regime: str = "unknown"
        self.sync_cursor: int = 0  # exchange TradesHistory cursor (seconds) for live ingest
        self._seen: set[str] = set()  # txids already credited to expectancy (dedup)
        self.load()

    # ── persistence ────────────────────────────────────────────
    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        self.last_regime = data.get("last_regime", "unknown")
        self.sync_cursor = int(data.get("sync_cursor", 0))
        self._seen = set(data.get("seen_txids", []))
        for sym, c in (data.get("coins") or {}).items():
            self.coins[sym] = CoinStats(
                symbol=sym,
                trades=int(c.get("trades", 0)),
                wins=int(c.get("wins", 0)),
                pnl=float(c.get("pnl", 0.0)),
                best_regime=c.get("best_regime", "unknown"),
                regime_pnl=dict(c.get("regime_pnl", {})),
            )

    def save(self) -> None:
        data = {
            "last_regime": self.last_regime,
            "sync_cursor": self.sync_cursor,
            "seen_txids": sorted(self._seen)[-10_000:],
            "coins": {
                sym: {
                    "trades": c.trades,
                    "wins": c.wins,
                    "pnl": round(c.pnl, 4),
                    "best_regime": c.best_regime,
                    "regime_pnl": c.regime_pnl,
                }
                for sym, c in self.coins.items()
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # ── live exchange ingest ──────────────────────────────────
    def sync_from_exchange(self, gateway) -> int:
        """Pull REAL closed-trade P&L from Kraken and feed it into the learner.

        Returns the number of *new* trades ingested. Must be guarded by the
        caller — any failure (auth, rate limit, offline) is swallowed and
        returns 0 so the trading cycle is never blocked by the learning sync.
        De-duplicates by Kraken txid so a re-fetch after a restart (or a wide
        ``since`` window) never double-counts realized P&L.
        """
        if not self.enabled:
            return 0
        try:
            rows, cursor = gateway.closed_trade_pnl(since=self.sync_cursor)
        except Exception:
            return 0
        if not rows:
            return 0
        ingested = 0
        for t in rows:
            txid = t.get("txid")
            if txid and txid in self._seen:
                continue
            self.record_trade(t["symbol"], t["pnl"], self.last_regime)
            if txid:
                self._seen.add(txid)
            ingested += 1
        self.sync_cursor = cursor
        # Bound the seen set so the persisted file cannot grow without limit.
        if len(self._seen) > 20_000:
            self._seen = set(sorted(self._seen)[-10_000:])
        self.save()
        return ingested

    # ── recording outcomes ────────────────────────────────────
    def record_trade(self, symbol: str, pnl: float, regime: str = "unknown") -> None:
        """Record a closed trade's realized P&L for ``symbol`` under ``regime``."""
        if not self.enabled:
            return
        sym = symbol.upper().strip()
        c = self.coins.get(sym) or CoinStats(symbol=sym)
        c.trades += 1
        if pnl > 0:
            c.wins += 1
        c.pnl += pnl
        c.regime_pnl[regime] = c.regime_pnl.get(regime, 0.0) + pnl
        # Track the regime where this coin has been most profitable.
        if not c.best_regime or c.regime_pnl.get(regime, 0.0) >= c.regime_pnl.get(c.best_regime, 0.0):
            c.best_regime = regime
        self.coins[sym] = c
        self.last_regime = regime
        self.save()

    # ── bias for selection ────────────────────────────────────
    def bias(self, symbol: str) -> float:
        """Selection multiplier in [0.5, 1.5].

        1.0 = no learned opinion (too few trades, or disabled).
        >1.0 = coin has positive expectancy -> favored.
        <1.0 = coin bleeds -> avoided.
        """
        if not self.enabled:
            return 1.0
        c = self.coins.get(symbol.upper().strip())
        if not c or c.trades < self.min_trades:
            return 1.0  # not enough evidence to bias
        # Map expectancy (avg $/trade) to a gentle multiplier, clamped.
        # ~+$0.50/trade -> +0.5 bias; ~-$0.50/trade -> -0.5 bias.
        mult = 1.0 + max(-0.5, min(0.5, c.expectancy * 1.0))
        return round(mult, 3)

    def regime_penalty(self, symbol: str, regime: str) -> float:
        """Extra penalty (multiplier in (0,1]) if this coin bleeds in ``regime``."""
        if not self.enabled:
            return 1.0
        c = self.coins.get(symbol.upper().strip())
        if not c or c.trades < self.min_trades:
            return 1.0
        rp = c.regime_pnl.get(regime, 0.0)
        if rp < 0:
            return 0.6  # avoid coins that lose in the current regime
        return 1.0

    def summary(self) -> dict[str, Any]:
        """Human-readable learning state for the dashboard / logs."""
        ranked = sorted(
            self.coins.values(), key=lambda c: c.expectancy, reverse=True
        )
        return {
            "enabled": self.enabled,
            "min_trades": self.min_trades,
            "last_regime": self.last_regime,
            "coins_tracked": len(self.coins),
            "best": [
                {
                    "symbol": c.symbol,
                    "trades": c.trades,
                    "win_rate_pct": round(c.win_rate * 100, 1),
                    "expectancy_usd": round(c.expectancy, 3),
                    "best_regime": c.best_regime,
                }
                for c in ranked[:5]
            ],
            "worst": [
                {
                    "symbol": c.symbol,
                    "trades": c.trades,
                    "win_rate_pct": round(c.win_rate * 100, 1),
                    "expectancy_usd": round(c.expectancy, 3),
                }
                for c in ranked[-3:]
            ],
        }

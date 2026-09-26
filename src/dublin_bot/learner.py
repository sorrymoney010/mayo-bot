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
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── adaptive gating knobs (overridable per LearningAgent instance) ──────────
ROLLING_WINDOW = 20          # closed trades per symbol / regime kept for expectancy
MIN_SAMPLE = 8               # closed trades needed before a bench can trigger
PRIOR_MAX_WEIGHT = 5.0       # backtest prior counts as at most N pseudo-trades
BENCH_HOURS = 72.0           # benched symbol/regime sits out this long, then probation
PROBATION_SIZE = 0.25        # size multiplier for the first trade after a bench
WEAK_EDGE_BPS = 25.0         # 0 <= expectancy < this → "weak" → reduced size


@dataclass
class CoinStats:
    symbol: str
    trades: int = 0
    wins: int = 0
    pnl: float = 0.0
    best_regime: str = "unknown"
    # Per-regime P&L, used to skip a coin in regimes where it bleeds.
    regime_pnl: dict[str, float] = field(default_factory=dict)
    # Rolling closed-trade history (newest last): {"pnl","net_bps","regime","ts","strategy"}.
    # Only trades recorded with a notional carry net_bps; legacy trades don't.
    history: list[dict] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return (self.wins / self.trades) if self.trades else 0.0

    @property
    def expectancy(self) -> float:
        """Average realized P&L per closed trade (the thing we actually want > 0)."""
        return (self.pnl / self.trades) if self.trades else 0.0


@dataclass
class GateDecision:
    """Adaptive verdict for a symbol (and optionally a regime) before a new entry."""

    key: str
    allow: bool = True
    size_mult: float = 1.0
    state: str = "neutral"        # neutral | ok | weak | negative | benched | probation
    reason: str = "no evidence yet"
    live_n: int = 0
    live_bps: float | None = None
    prior_n: float = 0.0
    prior_bps: float | None = None
    blended_bps: float | None = None

    def to_dict(self) -> dict[str, Any]:
        def r(x):
            return None if x is None else round(float(x), 2)
        return {
            "key": self.key, "allow": self.allow, "size_mult": self.size_mult,
            "state": self.state, "reason": self.reason, "live_n": self.live_n,
            "live_bps": r(self.live_bps), "prior_n": r(self.prior_n),
            "prior_bps": r(self.prior_bps), "blended_bps": r(self.blended_bps),
        }


class LearningAgent:
    def __init__(self, path: str | Path, min_trades: int = 3, enabled: bool = True,
                 *, strategy_key: str = "default", priors_path: str | Path | None = None,
                 window: int = ROLLING_WINDOW, min_sample: int = MIN_SAMPLE,
                 bench_hours: float = BENCH_HOURS):
        self.path = Path(path)
        self.min_trades = min_trades
        self.enabled = enabled
        self.strategy_key = strategy_key
        self.window = int(window)
        self.min_sample = int(min_sample)
        self.bench_hours = float(bench_hours)
        self.coins: dict[str, CoinStats] = {}
        self.last_regime: str = "unknown"
        self.sync_cursor: int = 0  # exchange TradesHistory cursor (seconds) for live ingest
        self._seen: set[str] = set()  # txids already credited to expectancy (dedup)
        # key ("SYM" or "SYM|regime") -> {"until": ts, "since": ts, "trades_at_bench": n, "reason"}
        self.benches: dict[str, dict] = {}
        self.last_decisions: dict[str, str] = {}
        # symbol -> {"regime","notional","ts","strategy"} captured at paper BUY
        self.open_entries: dict[str, dict] = {}
        self.decisions_log = self.path.parent / "learner_decisions.jsonl"
        self.priors: dict[str, dict] = {}
        self.load()
        if priors_path is not None:
            self.load_priors(priors_path)

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
        self.benches = dict(data.get("benches") or {})
        self.last_decisions = dict(data.get("last_decisions") or {})
        self.open_entries = dict(data.get("open_entries") or {})
        for sym, c in (data.get("coins") or {}).items():
            self.coins[sym] = CoinStats(
                symbol=sym,
                trades=int(c.get("trades", 0)),
                wins=int(c.get("wins", 0)),
                pnl=float(c.get("pnl", 0.0)),
                best_regime=c.get("best_regime", "unknown"),
                regime_pnl=dict(c.get("regime_pnl", {})),
                history=list(c.get("history", [])),
            )

    def save(self) -> None:
        data = {
            "last_regime": self.last_regime,
            "sync_cursor": self.sync_cursor,
            "seen_txids": sorted(self._seen)[-10_000:],
            "strategy_key": self.strategy_key,
            "benches": self.benches,
            "last_decisions": self.last_decisions,
            "open_entries": self.open_entries,
            "coins": {
                sym: {
                    "trades": c.trades,
                    "wins": c.wins,
                    "pnl": round(c.pnl, 4),
                    "best_regime": c.best_regime,
                    "regime_pnl": c.regime_pnl,
                    "history": c.history[-200:],
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
    def record_trade(self, symbol: str, pnl: float, regime: str = "unknown", *,
                     notional: float | None = None, strategy: str | None = None,
                     ts: float | None = None) -> None:
        """Record a closed trade's realized P&L for ``symbol`` under ``regime``.

        ``notional`` (cost basis of the lot) lets the learner track size-free
        net return in bps — the unit the adaptive gate and backtest priors use.
        """
        if not self.enabled:
            return
        sym = symbol.upper().strip()
        c = self.coins.get(sym) or CoinStats(symbol=sym)
        net_bps = None
        if notional is not None and notional > 0 and math.isfinite(float(pnl)):
            net_bps = float(pnl) / float(notional) * 1e4
        c.history.append({
            "pnl": round(float(pnl), 6),
            "net_bps": None if net_bps is None else round(net_bps, 2),
            "regime": regime,
            "ts": float(ts if ts is not None else time.time()),
            "strategy": strategy or self.strategy_key,
        })
        c.history = c.history[-200:]
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
    def update_regime(self, regime: str, *, persist: bool = True) -> None:
        """Persist the latest classified market regime for bias/penalty lookups.

        Called once per engine cycle after ``classify_regime`` so ``last_regime``
        is no longer stuck at ``"unknown"``. Empty/None inputs are ignored.
        """
        if not regime:
            return
        normalized = str(regime).strip().lower() or "unknown"
        if self.last_regime == normalized:
            return
        self.last_regime = normalized
        if persist and self.enabled:
            self.save()

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

    # ── adaptive gate: rolling expectancy + backtest priors ───────────
    def load_priors(self, path: str | Path) -> int:
        """Load per-symbol / per-regime OOS expectancy from the walk-forward JSON.

        Only rows matching ``strategy_key`` ("<family>@<tf>m") are used, so a
        breakout@15m backtest never masquerades as evidence for regime@60m.
        Returns the number of priors loaded (0 if the file is missing/bad).
        """
        self.priors = {}
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        for row in data.get("results", []):
            key = f"{row.get('family')}@{row.get('tf')}m"
            if key != self.strategy_key:
                continue
            sym = str(row.get("symbol", "")).upper()
            oos = row.get("oos") or {}
            if int(oos.get("trades", 0)) > 0:
                self.priors[sym] = {"n": int(oos["trades"]), "bps": float(oos["avg_net_bps"])}
            for reg, m in (row.get("oos_by_regime") or {}).items():
                if int(m.get("trades", 0)) > 0:
                    self.priors[f"{sym}|{reg}"] = {"n": int(m["trades"]),
                                                   "bps": float(m["avg_net_bps"])}
        return len(self.priors)

    def _live(self, symbol: str, regime: str | None = None) -> tuple[list[float], int]:
        """(last-window net_bps list, total matching trades) for this strategy."""
        c = self.coins.get(symbol.upper().strip())
        if not c:
            return [], 0
        rows = [
            h for h in c.history
            if h.get("net_bps") is not None
            and h.get("strategy") == self.strategy_key
            and (regime is None or h.get("regime") == regime)
        ]
        return [float(h["net_bps"]) for h in rows[-self.window:]], len(rows)

    def _evaluate_key(self, key: str, symbol: str, regime: str | None, now: float) -> GateDecision:
        d = GateDecision(key=key)
        live, total = self._live(symbol, regime)
        d.live_n = len(live)
        d.live_bps = (sum(live) / len(live)) if live else None
        prior = self.priors.get(key)
        w = 0.0
        if prior:
            w = min(float(prior["n"]), PRIOR_MAX_WEIGHT)
            d.prior_n, d.prior_bps = float(prior["n"]), float(prior["bps"])
        if live or w > 0:
            d.blended_bps = ((d.prior_bps or 0.0) * w + sum(live)) / (w + len(live))

        bench = self.benches.get(key)
        if bench:
            if now < float(bench["until"]):
                d.allow, d.size_mult, d.state = False, 0.0, "benched"
                d.reason = (f"benched until {time.strftime('%Y-%m-%d %H:%M', time.gmtime(bench['until']))}Z: "
                            f"{bench.get('reason', '')}")
                return d
            if total <= int(bench.get("trades_at_bench", 0)):
                d.allow, d.size_mult, d.state = True, PROBATION_SIZE, "probation"
                d.reason = f"bench expired; probation trade at {PROBATION_SIZE:g}x size"
                return d
            # A probation trade has closed: re-judge on the rolling window.
            if d.live_bps is not None and d.live_bps < 0 and d.live_n >= self.min_sample:
                self._bench(key, now, total, f"still negative after probation "
                                             f"({d.live_bps:.0f}bps over {d.live_n})")
                d.allow, d.size_mult, d.state = False, 0.0, "benched"
                d.reason = self.benches[key]["reason"]
                return d
            self.benches.pop(key, None)

        if d.live_n >= self.min_sample and d.live_bps is not None and d.live_bps < 0:
            self._bench(key, now, total, f"expectancy {d.live_bps:.0f}bps/trade after fees "
                                         f"over last {d.live_n} closed trades")
            d.allow, d.size_mult, d.state = False, 0.0, "benched"
            d.reason = self.benches[key]["reason"]
            return d

        if d.blended_bps is None:
            d.state, d.size_mult, d.reason = "neutral", 1.0, "no live trades or backtest prior yet"
        elif d.blended_bps < 0:
            d.state, d.size_mult = "negative", 0.5
            d.reason = (f"blended expectancy {d.blended_bps:.0f}bps (live n={d.live_n}, "
                        f"prior n={d.prior_n:g}) < 0 → half size until sample >= {self.min_sample}")
        elif d.blended_bps < WEAK_EDGE_BPS:
            d.state, d.size_mult = "weak", 0.75
            d.reason = f"weak edge {d.blended_bps:.0f}bps < {WEAK_EDGE_BPS:g}bps → 0.75x size"
        else:
            d.state, d.size_mult = "ok", 1.0
            d.reason = f"edge {d.blended_bps:.0f}bps (live n={d.live_n}, prior n={d.prior_n:g})"
        return d

    def _bench(self, key: str, now: float, total: int, reason: str) -> None:
        self.benches[key] = {
            "since": now, "until": now + self.bench_hours * 3600.0,
            "trades_at_bench": int(total), "reason": reason,
        }

    def gate(self, symbol: str, regime: str | None = None, *,
             now: float | None = None, persist: bool = True) -> GateDecision:
        """Combined symbol + regime verdict for a NEW entry. Exits are never gated."""
        sym = symbol.upper().strip()
        if not self.enabled:
            return GateDecision(key=sym, reason="learner disabled")
        now = float(now if now is not None else time.time())
        sd = self._evaluate_key(sym, sym, None, now)
        out = sd
        if regime:
            rd = self._evaluate_key(f"{sym}|{regime}", sym, regime, now)
            if not rd.allow or (sd.allow and rd.size_mult < sd.size_mult):
                out = GateDecision(**{**sd.__dict__})
                out.allow = sd.allow and rd.allow
                out.size_mult = min(sd.size_mult, rd.size_mult) if out.allow else 0.0
                out.state = rd.state if not rd.allow or rd.size_mult < sd.size_mult else sd.state
                out.reason = f"regime {regime}: {rd.reason}" if not rd.allow or rd.size_mult < sd.size_mult else sd.reason
                out.key = rd.key if not rd.allow else sd.key
        self._log_change(out, now)
        if persist:
            self.save()
        return out

    def _log_change(self, d: GateDecision, now: float) -> None:
        label = f"{d.state}:{d.size_mult:g}"
        if self.last_decisions.get(d.key) == label:
            return
        self.last_decisions[d.key] = label
        try:
            self.decisions_log.parent.mkdir(parents=True, exist_ok=True)
            with self.decisions_log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": now, "strategy": self.strategy_key,
                                     **d.to_dict()}) + "\n")
        except OSError:
            pass

    def note_entry(self, symbol: str, *, regime: str, notional: float) -> None:
        """Remember the entry regime + cost basis so the exit is booked correctly."""
        if not self.enabled:
            return
        self.open_entries[symbol.upper().strip()] = {
            "regime": regime, "notional": float(notional), "ts": time.time(),
            "strategy": self.strategy_key,
        }
        self.save()

    def pop_entry(self, symbol: str) -> dict | None:
        return self.open_entries.pop(symbol.upper().strip(), None)

    def adaptive_summary(self, symbols: list[str], regime: str | None = None) -> list[dict]:
        return [self.gate(s, regime, persist=False).to_dict() for s in symbols]

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

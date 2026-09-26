"""Trading engine: the ordered safety pipeline for one decision cycle.

Every cycle runs the same gate sequence, and **any** gate failing aborts before
an order can be formed:

    1. safety locks        — paper/dry-run/live flags consistent
    2. market data         — bars fetched, monotonic, no duplicates
    3. freshness           — bar age and exchange clock skew within tolerance
    4. market quality      — spread and liquidity acceptable
    5. strategy signal     — indicator confluence
    6. risk manager        — sizing, circuit breakers, cooldown, order caps
    7. precision           — exchange minimums and lot rounding
    8. idempotency         — this exact intent has not been submitted before
    9. execution           — dry-run synthetic id, or gated live submission

Gate order is deliberate: cheap local checks precede network calls, and the
idempotency reservation happens immediately before execution so the persisted
window of "unknown outcome" is as small as possible.
"""

from __future__ import annotations

import json
import os
import tempfile
import pandas as pd
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


from .audit import AuditEvent, AuditLog
from .config import Settings
from .sentiment import SentimentAgent
from .errors import (
    BrokerError,
    DuplicateOrderError,
    PrecisionError,
    SafetyLockError,
    StaleDataError,
)
from .fills import FillModel
from .idempotency import IdempotencyLedger, make_intent_key
from .journal import Journal
from .kraken_gateway import KrakenGateway
from .binance_gateway import BinanceGateway
from .market_quality import MarketGuard, MarketQuality
from .models import Action, DecisionRecord, RiskDecision, Signal
from .paper import PaperPortfolio
from .risk import RiskManager
from .state import StateStore
from .strategy import build_strategy
from .emergency import emergency_stop_active
from .dca import DCAAccumulator


def _norm_pair(value: str) -> str:
    """Normalize a Kraken pair for comparison (XBTUSD == XXBTZUSD == XBT/USD).

    Kraken reports the same pair under several spellings — OpenOrders uses the
    altname (``XBTUSD``), metadata uses the canonical key (``XXBTZUSD``), and
    config uses ``BTC/USD`` with ``BTC`` aliased to ``XBT``. Compare normalized
    forms so a symbol mismatch never rejects the bot's own order.
    """
    raw = str(value).upper().replace("/", "").replace("-", "").replace("_", "")
    # Kraken spells the same pair three ways: canonical key "XXBTZUSD"
    # (X-prefixed base + Z-prefixed fiat quote), altname "XBTUSD", and
    # wsname/config "BTC/USD". Reduce all of them to the altname form.
    for plain, kraken in (("BTC", "XBT"), ("DOGE", "XDG")):
        if raw.startswith(plain):
            raw = kraken + raw[len(plain):]
    if raw.startswith("XX"):
        raw = raw[1:]              # XXBTZUSD -> XBTZUSD
    if raw.endswith("ZUSD"):
        raw = raw[:-4] + "USD"     # XBTZUSD  -> XBTUSD
    return raw


def build_gateway(settings: Settings, **kwargs):
    """Factory: returns the configured broker gateway.

    Supported: kraken (Spot), binance (Spot).  Each adapter is a drop-in
    implementation of the BrokerGateway protocol, so the engine, risk manager,
    dashboard and audit log stay broker-agnostic.
    """
    if settings.broker == "kraken":
        kwargs.setdefault("allow_order_submission", bool(settings.allow_live_trading and not settings.paper_trading and not settings.dry_run))
        return KrakenGateway(settings, **kwargs)
    if settings.broker == "binance":
        kwargs.setdefault("allow_order_submission", bool(settings.allow_live_trading and not settings.paper_trading and not settings.dry_run))
        return BinanceGateway(settings, **kwargs)
    raise ValueError(
        f"Unsupported broker: {settings.broker!r}. "
        "Supported: 'kraken', 'binance'."
    )


@dataclass
class CycleResult:
    """Full outcome of one engine cycle, including why it stopped."""

    record: DecisionRecord | None
    blocked_at: str | None = None
    block_reason: str | None = None
    gates: dict[str, object] = field(default_factory=dict)

    @property
    def executed(self) -> bool:
        return self.record is not None and self.record.order_id is not None

    def to_dict(self) -> dict:
        return {
            "blocked_at": self.blocked_at,
            "block_reason": self.block_reason,
            "gates": self.gates,
            "decision": self.record.to_dict() if self.record else None,
        }


class TradingEngine:
    def __init__(self, settings: Settings, gateway=None, audit: AuditLog | None = None) -> None:
        self.settings = settings
        self.audit = audit or AuditLog(Path(settings.audit_log_path))
        self.gateway = gateway or build_gateway(settings, audit=self.audit)
        self.strategy = build_strategy(settings)
        self.risk = RiskManager(settings)
        self.journal = Journal(settings.journal_path)
        self.state_store = StateStore(Path(settings.session_state_path))
        self.ledger = IdempotencyLedger(Path(settings.idempotency_path))
        self.market_guard = MarketGuard(
            max_spread_bps=settings.max_spread_bps,
            min_dollar_volume=settings.min_dollar_volume,
        )
        self.fill_model = FillModel(settings)
        self.paper_portfolio = PaperPortfolio(Path("logs/paper_portfolio.json"))
        self.sentiment = SentimentAgent()
        self._rotation = 0  # retained for backward compat; selection is now score-driven
        self.dca = DCAAccumulator(settings)
        self.dca._state_path = settings.dca_state_path
        self._dca_state = self.dca.load(settings.dca_state_path)
        self._dca_executed_this_cycle = False
        self._dca_notional = 0.0
        # Bot-owned lot ledger: symbol -> quantity the bot actually acquired,
        # so a SELL never liquidates holdings the bot did not purchase.
        # Paper/dry-run use a separate file so live never reads paper lots.
        if settings.paper_trading or settings.dry_run:
            self._bot_qty_path = Path("logs/paper_bot_positions.json")
        else:
            self._bot_qty_path = Path("logs/bot_positions.json")
        self._bot_qty: dict[str, float] = self._load_bot_qty()
        # Drop lots the exchange no longer holds before any strategy call, so a
        # stale phantom lot can never freeze the bot into exit-only WAIT.
        self._reconcile_bot_qty()
        # Paper loop creates a fresh engine each cycle; hydrate from the paper
        # portfolio when the paper ledger is empty so open lots can still exit.
        if (settings.paper_trading or settings.dry_run) and not self._bot_qty:
            self._hydrate_bot_qty_from_paper_portfolio()
        # Bracket parent userrefs by bot-owned symbol. A later exit can then
        # cancel only the stop/target created for that exact bot-owned lot.
        self._bracket_refs_path = Path("logs/bot_brackets.json")
        self._bracket_refs: dict[str, dict[str, object]] = self._load_bracket_refs()
        # Backfill any bracket whose child txids were never captured, so an
        # exit can still cancel its own stop instead of failing closed forever.
        self._reconcile_bracket_refs()
        # Pending rotation target (single-position aggressive model): when set,
        # the bot exits the held bot-owned lot on the current symbol and the
        # next cycle enters this coin.
        self._rotate_to: str | None = None
        from .learner import LearningAgent
        self.learner = LearningAgent(
            settings.learner_path,
            min_trades=settings.learner_min_trades,
            enabled=settings.learner_enabled,
            strategy_key=self.strategy_key(),
            priors_path=getattr(settings, "learner_priors_path", None),
            min_sample=int(getattr(settings, "learner_min_sample", 8)),
            bench_hours=float(getattr(settings, "learner_bench_hours", 72.0)),
        )
        self._learner_size_mult = 1.0
        self._entry_regime = "unknown"
        # Day-trade mode: tighten data resolution + monitor cadence so the bot
        # reacts intraday. Strategy gates are NOT loosened — only speed.
        self._apply_performance_profile()

        # Real-time feed (WebSocket) — read-only, best-effort. Lets the bot
        # check live price between REST cycles for protective-stop triggers.
        # Only spun up in day-trade mode (otherwise the 15m REST cadence is
        # enough).  Falls back to REST ticker if the socket is down.
        self._feed = None
        if getattr(self.settings, "day_trade_mode", False):
            try:
                from .realtime import KrakenRealtimeFeed
                wsname = self._kraken_wsname()
                self._feed = KrakenRealtimeFeed({self.settings.symbol: wsname})
                self._feed.start()
            except Exception:
                self._feed = None

    def _kraken_wsname(self) -> str:
        """Kraken wsname (e.g. 'XBT/USD') for the active symbol, else canonical."""
        try:
            return self.gateway.resolve_symbol().wsname or self.settings.symbol
        except Exception:
            return self.settings.symbol

    def _apply_performance_profile(self) -> None:
        """Coerce timeframe/cadence from the high-level mode flags.

        day_trade_mode -> 5m bars, 5m candle-close cadence. Lets the operator flip
        intraday behaviour with one boolean instead of three coupled knobs.
        """
        s = self.settings
        if getattr(s, "day_trade_mode", False):
            s.timeframe_minutes = 5
            s.monitor_interval_seconds = 300

    def _refresh_live_realized_pnl(self, state) -> bool:
        """Set today's realized P&L from Kraken's closed-trade ledger.

        Account equity changes also include deposits, withdrawals, and external
        holdings. They must never be treated as trading losses and trip the
        entry circuit breaker. Paper mode updates this value from simulated
        fills in ``_execute`` instead.
        """
        if self.settings.paper_trading or self.settings.dry_run:
            return True
        closed_trade_pnl = getattr(self.gateway, "closed_trade_pnl", None)
        if not callable(closed_trade_pnl):
            return False
        local_midnight = datetime.now().astimezone().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        try:
            result = closed_trade_pnl(since=int(local_midnight.timestamp()))
        except Exception:
            return False
        if not isinstance(result, tuple) or len(result) != 2:
            return False
        rows = result[0]
        if not isinstance(rows, list):
            return False
        realized_pnl = 0.0
        for row in rows:
            if not isinstance(row, dict):
                return False
            try:
                realized_pnl += float(row["pnl"])
            except (KeyError, TypeError, ValueError):
                return False
        state.realized_pnl_today = realized_pnl
        return True

    # ── gate 1: safety ───────────────────────────────────────

    def _can_size(self, symbol: str, notional: float) -> bool:
        """True if the gateway can actually place a ``notional`` order for ``symbol``.

        Uses the real ``size_buy`` (precision/lot rules) rather than an estimate,
        so a coin whose lot minimum rounds above ``notional`` is excluded instead
        of throwing ``PrecisionError`` mid-execution on a small account.
        """
        saved = self.settings.symbol
        try:
            self.settings.symbol = symbol
            self.gateway.size_buy(notional)
            return True
        except Exception:
            return False
        finally:
            self.settings.symbol = saved


    def _is_breakout_sleeve(self) -> bool:
        """Multi-symbol defined-risk sleeves (breakout + regime-switch trend)."""
        name = str(getattr(self.settings, "strategy", "") or "").lower()
        return name in ("breakout", "momentum_breakout", "regime_trend", "regime")

    def _is_regime_sleeve(self) -> bool:
        name = str(getattr(self.settings, "strategy", "") or "").lower()
        return name in ("regime_trend", "regime")

    def strategy_key(self) -> str:
        """Learner/backtest key, e.g. 'regime@60m' (matches walkforward JSON)."""
        name = str(getattr(self.settings, "strategy", "momentum") or "momentum").lower()
        family = {
            "breakout": "breakout", "momentum_breakout": "breakout",
            "regime_trend": "regime", "regime": "regime", "momentum": "momentum",
        }.get(name, name)
        return f"{family}@{int(self.settings.timeframe_minutes)}m"

    def _adx_regime(self, bars) -> str:
        try:
            from .backtest_core import classify_adx_regime
            return classify_adx_regime(
                bars,
                float(getattr(self.settings, "adx_enter_above", 25.0)),
                float(getattr(self.settings, "adx_exit_below", 20.0)),
            )
        except Exception:
            return "unknown"

    def _account_equity(self) -> float:
        """Sizing equity. Paper + PAPER_USE_LEDGER_EQUITY → paper ledger book
        (cash + open cost basis, seeded at STRATEGY_EQUITY_USD) so paper runs
        on its own budget instead of the real Kraken balance."""
        s = self.settings
        if getattr(s, "paper_use_ledger_equity", False) and (s.paper_trading or s.dry_run):
            path = Path(self.paper_portfolio.path)
            if not path.exists():
                return float(s.strategy_equity_usd)
            try:
                import json as _json
                data = _json.loads(path.read_text(encoding="utf-8"))
                cash = float(data.get("cash", s.strategy_equity_usd))
                basis = sum(
                    float(p.get("quantity", 0.0)) * float(p.get("entry_price", 0.0))
                    for p in data.get("positions", [])
                )
                return max(cash + basis, 0.0)
            except (OSError, ValueError, TypeError):
                return float(s.strategy_equity_usd)
        return self.gateway.account_equity()

    def _open_bot_position_count(self) -> int:
        return sum(1 for qty in self._bot_qty.values() if float(qty) > 1e-9)

    def _breakout_universe(self) -> list[str]:
        """Canonical BTC/ETH/SOL (or configured) list for the breakout sleeve."""
        s = self.settings
        raw = list(getattr(s, "breakout_symbols", None) or [])
        if not raw:
            raw = ["BTC/USD", "ETH/USD", "SOL/USD"]
        ordered: list[str] = []
        for sym in raw:
            sym = str(sym).strip().upper()
            if sym and sym not in ordered:
                ordered.append(sym)
        if s.universe_allowlist:
            allow = {x.upper() for x in s.universe_allowlist}
            ordered = [c for c in ordered if c in allow]
        return ordered

    def _paper_protective_would_exit(self, symbol: str, bars) -> str | None:
        """Return 'stop' / 'tp' if paper sleeve levels are hit, else None."""
        if not (self.settings.paper_trading or self.settings.dry_run):
            return None
        try:
            snap = self.paper_portfolio.snapshot()
            pos = snap.positions.get(symbol)
        except Exception:
            return None
        if pos is None or float(pos.quantity) <= 1e-9 or float(pos.entry_price) <= 0:
            return None
        entry = float(pos.entry_price)
        last = None
        try:
            if bars is not None and len(bars) and "close" in bars:
                last = float(bars["close"].iloc[-1])
        except Exception:
            last = None
        if last is None:
            try:
                saved = self.settings.symbol
                self.settings.symbol = symbol
                try:
                    last = float(self.gateway.get_ticker()["last"])
                finally:
                    self.settings.symbol = saved
            except Exception:
                return None
        stop_pct = float(self.settings.stop_loss_pct)
        tp_pct = float(self.settings.take_profit_pct)
        if last <= entry * (1.0 - stop_pct):
            return "stop"
        if last >= entry * (1.0 + tp_pct):
            return "tp"
        return None

    def _select_symbol_breakout(self) -> None:
        """Multi-symbol scan for the defined-risk breakout sleeve.

        - Max 1 position per symbol (bot-qty ledger).
        - Max ``max_concurrent_positions`` open lots (default 3).
        - Prefer a held symbol that already hit paper stop/TP.
        - Else, if under the concurrent cap, pick the strongest fresh BUY
          among non-held symbols in ``breakout_symbols``.
        - Else monitor a held lot (round-robin by symbol name).
        Does not use rotate-out-to-enter; concurrent holdings are intentional.
        """
        s = self.settings
        universe = self._breakout_universe()
        if not universe:
            return
        held = [sym for sym, qty in self._bot_qty.items() if float(qty) > 1e-9]
        # 1) Exit priority: any held name that hit stop/TP.
        for sym in held:
            saved = s.symbol
            try:
                s.symbol = sym
                bars = self.gateway.get_bars()
            except Exception:
                s.symbol = saved
                continue
            hit = self._paper_protective_would_exit(sym, bars)
            if not hit and self._is_regime_sleeve():
                try:
                    if self.strategy.evaluate(bars, in_position=True).action is Action.SELL:
                        hit = "strategy_exit"
                except Exception:
                    hit = None
            if hit:
                self.audit.record(
                    AuditEvent.SIGNAL,
                    {"event": "breakout_exit_priority", "symbol": sym, "hit": hit},
                )
                return  # s.symbol already set to sym
            s.symbol = saved

        open_n = len(held)
        max_n = int(getattr(s, "max_concurrent_positions", 3) or 3)

        # 2) Room for a new entry — scan non-held names for BUY breakouts.
        if open_n < max_n:
            equity = self._account_equity()
            cap = equity * s.max_position_fraction
            candidates = [
                sym for sym in universe
                if sym not in held and self._can_size(sym, cap)
            ]
            if not candidates:
                candidates = [sym for sym in universe if sym not in held]
            best_sym: str | None = None
            best_score = -1.0
            saved = s.symbol
            try:
                for sym in candidates:
                    if self.settings.sentiment_enabled and self.sentiment.should_block_buy(sym):
                        continue
                    try:
                        s.symbol = sym
                        bars = self.gateway.get_bars()
                        sig = self.strategy.evaluate(bars, in_position=False)
                    except Exception:
                        continue
                    if sig.action is Action.BUY and getattr(s, "learner_gate_enabled", True):
                        verdict = self.learner.gate(sym, self._adx_regime(bars))
                        if not verdict.allow:
                            self.audit.record(AuditEvent.SIGNAL, {
                                "event": "learner_bench_skip", "symbol": sym,
                                **verdict.to_dict(),
                            })
                            continue
                    if sig.action is Action.BUY and float(sig.score) > best_score:
                        best_score = float(sig.score)
                        best_sym = sym
            finally:
                s.symbol = saved
            if best_sym is not None:
                s.symbol = best_sym
                self.audit.record(
                    AuditEvent.SIGNAL,
                    {
                        "event": "breakout_symbol_select",
                        "symbol": best_sym,
                        "score": best_score,
                        "open_positions": open_n,
                        "max_concurrent": max_n,
                    },
                )
                return

        # 3) At cap or no fresh BUY — monitor a held lot (stable sort).
        if held:
            held_sorted = sorted(held)
            # Prefer current symbol if still held; else first held.
            if s.symbol in held_sorted:
                return
            s.symbol = held_sorted[0]
            return

        # 4) Flat — default to first universe symbol so bars/gates still run.
        if s.symbol not in universe:
            s.symbol = universe[0]

    def _select_symbol(self) -> None:
        """Autonomously pick the best tradeable coin for THIS cycle.

        No manual coin section: the bot scans the full tradeable universe
        (every active Kraken */USD pair when universe_mode="all_usd", otherwise
        the configured basket), keeps only coins it can *actually size* at the
        current balance and that the sentiment filter does not block, and ranks
        them by live strategy setup quality weighted by the self-learning agent
        (favor coins with proven positive expectancy, avoid bleeders). It trades
        whatever coin the market is offering — no pinned list, no round-robin.

        If we already hold a BOT-OWNED position in the current symbol, we let the
        strategy exit it rather than churning. But if a DIFFERENT allowed coin
        shows a clearly stronger momentum setup, we rotate: flag the stronger
        coin so the cycle exits the current bot-owned lot and enters it.
        Deposits/external holdings the bot did not buy never count as a
        position, so they can't freeze the bot into exit-only mode.

        Breakout / momentum_breakout uses ``_select_symbol_breakout`` (multi-symbol
        scan with max concurrent positions) instead of single-lot rotation.
        """
        if self._is_breakout_sleeve():
            self._select_symbol_breakout()
            return
        s = self.settings
        # Position awareness is per-symbol and bot-owned only. Raw
        # gateway.has_position() sees ALL balances (incl. pre-existing PUMP),
        # which would falsely lock the bot into exit-only forever.
        held_sym = s.symbol if self._bot_qty.get(s.symbol, 0.0) > 1e-9 else None
        in_position = held_sym is not None
        # Prefer exiting an existing bot lot before hunting a new entry. The
        # paper loop recreates the engine each cycle and may land on a fresh
        # symbol while open lots still sit in the paper ledger.
        if not in_position:
            held_others = [sym for sym, qty in self._bot_qty.items() if float(qty) > 1e-9]
            if held_others:
                s.symbol = held_others[0]
                held_sym = s.symbol
                in_position = True

        # Never abandon an open BOT-OWNED position without a reason — but if a
        # stronger setup exists elsewhere, rotate into it.
        if in_position:
            if s.rotate_positions:
                self._maybe_rotate(held_sym)
            return

        equity = self._account_equity()
        cap = equity * s.max_position_fraction

        # 1) Build the candidate pool.
        # The full Kraken USD market is 600+ pairs; scoring every one per cycle
        # would hammer Kraken's public rate limit and the bot's own limiter. So
        # the live scan uses the vetted, affordable basket (plus the DCA coin)
        # as the candidate pool — the bot is NOT pinned to one coin, it freely
        # picks among tradeable coins by strategy + learned bias. list_usd_pairs
        # still provides genuine full-market discovery when universe_allowlist
        # or a raised scan_limit widens the pool.
        if s.universe_mode == "basket":
            candidates = list(s.coin_basket)
        else:  # all_usd: autonomous over the tradeable pool
            candidates = list(s.coin_basket) + [s.dca_symbol]
        # Apply hard safety allowlist if the operator set one.
        if s.universe_allowlist:
            candidates = [c for c in candidates if c.upper() in
                          {x.upper() for x in s.universe_allowlist}]

        # 2) Keep only coins we can truly size at the position cap.
        affordable = [sym for sym in candidates if self._can_size(sym, cap)]
        if not affordable:
            affordable = candidates  # nothing fits; let risk reject downstream

        # Cap the per-cycle scan to respect rate limits; bias-favored coins
        # (known positive expectancy) are tried first.
        if len(affordable) > s.universe_scan_limit:
            affordable.sort(key=lambda sym: -self.learner.bias(sym))
            affordable = affordable[: s.universe_scan_limit]

        # 3) Rank each by live setup quality × self-learning bias.
        # Momentum (the default aggressive strategy) scores 30-40 on a real
        # setup, so the rotation threshold is set below that to allow the bot
        # to switch to the strongest coin in the universe. Mean-reversion
        # scores 60; the threshold still admits it.
        BUY_THRESHOLD = 25
        best_sym: str | None = None
        best_score = -1.0
        scored: list[tuple[str, float]] = []
        regime = self.learner.last_regime
        for sym in affordable:
            if sym == s.symbol:
                continue
            if self.sentiment.should_block_buy(sym):
                continue
            saved = s.symbol
            try:
                s.symbol = sym
                bars = self.gateway.get_bars()
                sig = self.strategy.evaluate(bars, in_position=False)
            except Exception:
                s.symbol = saved
                continue
            finally:
                s.symbol = saved
            bias = self.learner.bias(sym) * self.learner.regime_penalty(sym, regime)
            effective = float(sig.score) * bias
            scored.append((sym, effective))
            if effective > best_score:
                best_score = effective
                best_sym = sym

        # 4) Switch only if a coin clears the BUY threshold (a real setup).
        if best_sym is not None and best_score >= BUY_THRESHOLD:
            s.symbol = best_sym
            self.audit.record(
                AuditEvent.SIGNAL,
                {"event": "symbol_switch", "symbol": best_sym,
                 "score": round(best_score, 2), "equity": round(equity, 2),
                 "candidates_scored": len(scored)},
            )

    def _maybe_rotate(self, held_sym: str) -> None:
        """Detect a stronger momentum setup on a different allowed coin.

        When the bot holds a bot-owned lot on ``held_sym`` but another allowed
        coin shows a clearly stronger setup (by ``rotate_min_score_gap``), flag
        ``self._rotate_to`` so the cycle exits the held lot and the next cycle
        enters the stronger coin. This is the "trade all things" behavior —
        the bot rotates into the best opportunity instead of sitting in one
        coin forever. Only bot-owned lots are ever sold.
        """
        s = self.settings
        if not s.rotate_positions:
            return
        equity = self._account_equity()
        cap = equity * s.max_position_fraction
        candidates = list(s.coin_basket) + [s.dca_symbol]
        if s.universe_allowlist:
            allowed = {x.upper() for x in s.universe_allowlist}
            candidates = [c for c in candidates if c.upper() in allowed]
        affordable = [c for c in candidates
                      if c != held_sym and self._can_size(c, cap)]
        regime = self.learner.last_regime
        best_sym: str | None = None
        best_score = -1.0
        saved = s.symbol
        try:
            for sym in affordable:
                if self.sentiment.should_block_buy(sym):
                    continue
                try:
                    s.symbol = sym
                    bars = self.gateway.get_bars()
                    sig = self.strategy.evaluate(bars, in_position=False)
                except Exception:
                    continue
                finally:
                    s.symbol = saved
                bias = self.learner.bias(sym) * self.learner.regime_penalty(sym, regime)
                eff = float(sig.score) * bias
                if eff > best_score:
                    best_score = eff
                    best_sym = sym
        finally:
            s.symbol = saved
        if best_sym is None or best_score < 25:
            return
        # Compare against the held coin's current hold strength.
        try:
            s.symbol = held_sym
            hbars = self.gateway.get_bars()
            hsig = self.strategy.evaluate(hbars, in_position=True)
            held_score = float(hsig.score)
        except Exception:
            held_score = 0.0
        finally:
            s.symbol = saved
        if best_score - held_score >= s.rotate_min_score_gap:
            self._rotate_to = best_sym
            self.audit.record(
                AuditEvent.SIGNAL,
                {"event": "rotation_detected", "from": held_sym, "to": best_sym,
                 "held_score": round(held_score, 2), "target_score": round(best_score, 2),
                 "gap": round(best_score - held_score, 2)},
            )

    def assert_safety_locks(self) -> dict:
        """Verify the declared safety posture is internally consistent.

        Catches the dangerous middle state where live trading has been half
        enabled — for example ``paper_trading=false`` with ``dry_run`` still on,
        or live allowed without the typed acknowledgement.
        """
        s = self.settings
        report = s.safety_report()
        if not s.paper_trading and not s.allow_live_trading:
            raise SafetyLockError(
                "Inconsistent safety config: paper_trading=false requires "
                "allow_live_trading=true"
            )
        if s.allow_live_trading and s.live_risk_acknowledgement != "I_ACCEPT_LIVE_TRADING_RISK":
            raise SafetyLockError(
                "allow_live_trading=true requires the exact acknowledgement string"
            )
        self.audit.record(AuditEvent.SAFETY_CHECK, report)
        return report

    # ── restart recovery ─────────────────────────────────────

    def recover(self) -> dict:
        """Resolve intents left ``pending`` by a crash before the next cycle.

        A pending record means the process died between reserving the intent and
        confirming the outcome, so we cannot know whether the order reached the
        exchange.  Each is resolved by querying Kraken for its userref: found →
        confirmed, definitively absent → failed (and therefore retryable).
        """
        pending = self.ledger.pending()
        resolved: list[dict] = []
        for record in pending:
            if record.dry_run:
                self.ledger.fail(record.key, "dry-run intent abandoned at restart")
                resolved.append({"key": record.key, "outcome": "failed_dry_run"})
                continue
            found = None
            if hasattr(self.gateway, "find_order_by_userref"):
                try:
                    found = self.gateway.find_order_by_userref(record.userref)
                except BrokerError as exc:
                    resolved.append({"key": record.key, "outcome": "unresolved",
                                     "error": str(exc)})
                    continue
            if found:
                self.ledger.confirm(record.key, found["id"])
                resolved.append({"key": record.key, "outcome": "confirmed",
                                 "order_id": found["id"]})
            else:
                self.ledger.fail(record.key, "no matching order found at exchange")
                resolved.append({"key": record.key, "outcome": "failed"})

        summary = {"pending_found": len(pending), "resolved": resolved}
        if pending:
            self.audit.record(AuditEvent.RECOVERY, summary, severity="warning")
        self.ledger.prune()
        return summary

    # ── main cycle ───────────────────────────────────────────

    def _min_notional(self, symbol: str) -> float:
        """Estimated minimum order notional (USD) for ``symbol``.

        Kraken's ``cost_min`` is often a small placeholder (~$0.5) and does not
        reflect the real floor, which is driven by the **lot-size** minimum
        (``order_min``) times price.  We therefore use ``order_min * last`` as the
        effective minimum notional; ``cost_min`` is only a fallback when no price
        is available.
        """
        try:
            meta = self.gateway.resolve_symbol(symbol)
        except Exception:
            return float("inf")
        try:
            last = float(self.gateway.get_ticker_for(symbol)["last"])
        except Exception:
            last = 0.0
        min_qty = float(meta.order_min)
        if last > 0:
            # Add a safety margin so the resulting order, after volume rounding
            # to the pair's precision, still clears Kraken's lot minimum. A bare
            # order_min*last can round DOWN just below the limit (e.g. 2196 <
            # 2200) and raise PrecisionError mid-execution.
            return min_qty * last * 1.05
        cost_min = float(meta.cost_min) if meta.cost_min else 0.0
        return cost_min if cost_min > 0 else float("inf")

    def _apply_adx_and_fee_gates(self, signal: Signal, bars) -> Signal:
        """HARD sit-out / fee floor for new BUY entries (momentum + shared).

        - ADX chop sit-out when strategy is momentum and ADX gate is enabled.
        - Fee-aware min edge: TP distance must clear ``min_edge_bps``.
        Does not alter SELLs or in-position handling. Prefer maker/limit path
        elsewhere — this only blocks micro / no-trend entries.
        """
        if signal.action is not Action.BUY:
            return signal
        s = self.settings
        # Fee / min-edge gate (all strategies): block micro targets vs Kraken fees.
        if getattr(s, "min_edge_gate_enabled", True):
            from .indicators import fee_edge_ok
            ok, reason = fee_edge_ok(
                float(getattr(s, "take_profit_pct", 0.0)),
                float(getattr(s, "min_edge_bps", 100.0)),
            )
            if not ok:
                self.audit.record(AuditEvent.SIGNAL, {
                    "event": "fee_edge_block",
                    "reason": reason,
                    "take_profit_pct": float(getattr(s, "take_profit_pct", 0.0)),
                    "min_edge_bps": float(getattr(s, "min_edge_bps", 100.0)),
                })
                return Signal(
                    Action.WAIT, signal.score, reason,
                    signal.price, signal.atr, signal.stop_price,
                )

        # ADX sit-out: momentum only. Breakout soft-disables ADX (volume+high
        # break is the gate); mean-reversion may want chop.
        strat = str(getattr(s, "strategy", "momentum") or "momentum")
        if getattr(s, "adx_gate_enabled", True) and strat == "momentum":
            from .indicators import adx_trend_allowed, enrich
            try:
                frame = enrich(
                    bars,
                    fast_ema=s.fast_ema,
                    slow_ema=s.slow_ema,
                    regime_ema=s.regime_ema,
                    rsi_period=s.rsi_period,
                    atr_period=s.atr_period,
                    breakout_lookback=s.breakout_lookback,
                    volume_lookback=s.volume_lookback,
                    adx_period=getattr(s, "adx_period", 14),
                )
                adx_series = frame["adx"].dropna()
                allowed = adx_trend_allowed(
                    adx_series.tolist(),
                    enter_above=float(getattr(s, "adx_enter_above", 25.0)),
                    exit_below=float(getattr(s, "adx_exit_below", 20.0)),
                    initial=False,
                )
            except Exception as exc:
                self.audit.record(AuditEvent.SIGNAL, {
                    "event": "adx_gate_error", "error": str(exc),
                }, severity="warning")
                return signal
            if not allowed:
                adx_now = float("nan")
                if len(adx_series):
                    adx_now = float(adx_series.iloc[-1])
                adx_txt = f"{adx_now:.1f}" if adx_now == adx_now else "n/a"
                reason = f"ADX sit-out: chop (adx={adx_txt})"
                self.audit.record(AuditEvent.SIGNAL, {
                    "event": "adx_sit_out",
                    "reason": reason,
                    "adx": adx_now if adx_now == adx_now else None,
                    "enter_above": float(getattr(s, "adx_enter_above", 25.0)),
                    "exit_below": float(getattr(s, "adx_exit_below", 20.0)),
                })
                return Signal(
                    Action.WAIT, signal.score, reason,
                    signal.price, signal.atr, signal.stop_price,
                )
        return signal

    def run_cycle(self) -> CycleResult:

        gates: dict[str, object] = {}
        self.last_cycle_gates = gates  # exposed for the paper loop's log line
        # Snapshot the active symbol at cycle start so any temporary swap (e.g.
        # the DCA sleeve pointing one execution at PUMP/USD) is fully undone by
        # the end of the cycle, independent of in-cycle rotation.
        self._cycle_symbol_snapshot = self.settings.symbol

        # Gate 0 — self-learning sync (best-effort, never blocks trading).
        # Ingest REAL closed-trade P&L from Kraken so the learner's coin bias
        # reflects live results, not just paper fills.
        try:
            self.learner.sync_from_exchange(self.gateway)
        except Exception as exc:
            self.audit.record(AuditEvent.BROKER_ERROR,
                              {"operation": "learner_sync", "error": str(exc)},
                              severity="warning")

        # Gate 1 — safety locks
        try:
            gates["safety"] = self.assert_safety_locks()
        except SafetyLockError as exc:
            self.audit.record(AuditEvent.SAFETY_VIOLATION, {"error": str(exc)},
                              severity="critical")
            return CycleResult(None, "safety", str(exc), gates)
        if emergency_stop_active():
            return CycleResult(None, "emergency_stop", "Manual emergency stop is active", gates)

        # Gate 2 — symbol selection (adaptive to balance)
        self._select_symbol()
        gates["symbol"] = self.settings.symbol

        # Pending rotation: this cycle exits the held bot-owned lot, then points
        # the engine at the stronger coin so the NEXT cycle enters it. The SELL
        # branch below sells only the bot's own lot (never external holdings).
        rotation_target = self._rotate_to
        if rotation_target is not None:
            held = self._cycle_symbol_snapshot
            self.settings.symbol = held
            signal = Signal(
                Action.SELL, 90,
                f"Rotation exit {held} -> {rotation_target}", 0.0,
            )
            self.audit.record(AuditEvent.SIGNAL, {
                "event": "rotation_exit", "from": held, "to": rotation_target,
            })
            self._rotate_to = None
            # Let the cycle run the SELL; we'll repoint to the target after.

        # Restart recovery before any new intent can be formed.
        gates["recovery"] = self.recover()

        # Paper portfolio sync for display and realistic accounting.
        equity = self._account_equity()
        state = self.state_store.load(equity)
        state.current_equity = equity
        state.peak_equity = max(state.peak_equity, equity)
        self.paper_portfolio.load(equity=equity, cash=equity)

        # Gate 2 — market data
        try:
            bars = self.gateway.get_bars()
        except (BrokerError, StaleDataError) as exc:
            self.audit.record(AuditEvent.BROKER_ERROR,
                              {"operation": "get_bars", "error": str(exc)},
                              severity="error")
            return CycleResult(None, "market_data", str(exc), gates)
        gates["bars"] = {"count": int(len(bars))}

        # Gate 2b — regime classification → learner (offline, no network).
        # Prefer closes from the just-fetched bars so last_regime moves off
        # "unknown" even when the decision journal is still empty.
        try:
            from .learning import classify_regime
            closes = [float(x) for x in bars["close"].tolist()] if "close" in bars else []
            regime_info = classify_regime([], closes)
            self.learner.update_regime(regime_info.regime)
            gates["regime"] = {
                "regime": regime_info.regime,
                "confidence": regime_info.confidence,
                "description": regime_info.description,
            }
        except Exception as exc:  # never block trading on learner/regime errors
            gates["regime"] = {"regime": self.learner.last_regime, "error": str(exc)}

        # Gate 3 — freshness
        if hasattr(self.gateway, "check_freshness"):
            verdict = self.gateway.check_freshness(bars)
            gates["freshness"] = verdict.to_dict()
            if not verdict.fresh:
                return CycleResult(None, "freshness", verdict.reason, gates)

        # Gate 4 — market quality (advisory when the ticker is unavailable)
        quality_ok, quality_reason = True, "not evaluated"
        if hasattr(self.gateway, "market_quality"):
            try:
                snapshot = self.gateway.market_quality()
                quality_ok, quality_reason = self.market_guard.approve(
                    MarketQuality(
                        bid=snapshot["bid"],
                        ask=snapshot["ask"],
                        recent_dollar_volume=snapshot["recent_dollar_volume"],
                    )
                )
                gates["market_quality"] = {**snapshot, "approved": quality_ok,
                                           "reason": quality_reason}
                self.audit.record(AuditEvent.MARKET_QUALITY, gates["market_quality"])
            except BrokerError as exc:
                quality_ok, quality_reason = False, f"market quality unavailable: {exc}"
                gates["market_quality"] = {"approved": False, "reason": quality_reason}

        # Gate 5 — strategy signal (skipped when a rotation exit is already
        # scheduled — we don't want the held-coin strategy to overwrite the
        # forced SELL, nor the DCA sleeve to inject a buy this cycle).
        signal: Signal = Signal(Action.WAIT, 0, "no signal", 0.0)
        if rotation_target is None:
            # Per-symbol, bot-owned position awareness: the strategy is told the
            # bot owns the CURRENT symbol only if the bot itself bought it
            # (self._bot_qty). Raw gateway.has_position() sees ALL balances
            # (e.g. pre-existing XXBT dust), which would falsely lock the main
            # loop into exit-only mode and prevent new BTC entries.
            in_position = self._bot_qty.get(self.settings.symbol, 0.0) > 1e-9
            signal = self.strategy.evaluate(bars, in_position=in_position)
            self.audit.record(AuditEvent.SIGNAL, {
                "action": signal.action.value, "score": signal.score,
                "reason": signal.reason, "price": signal.price,
                "stop_price": signal.stop_price,
            })

            # Paper/dry-run protective stop & take-profit (no live brackets).
            if in_position and (self.settings.paper_trading or self.settings.dry_run):
                signal = self._apply_paper_protective_exit(signal, bars)

            # Gate 5.5 — sentiment confirmation filter (Stage 1).
            # Sentiment never originates a trade; it only (a) blocks a fresh BUY when
            # the coin's mood is bearish, and (b) forces a protective SELL when mood
            # collapses while in a position. This is what stops the bot from buying
            # into a coordinated dump / becoming reverse-pump exit liquidity.
            if self.settings.sentiment_enabled:
                sym = self.settings.symbol
                idx = self.sentiment.index_for(sym)
                if signal.action is Action.BUY and self.sentiment.should_block_buy(sym):
                    signal = Signal(
                        Action.WAIT, signal.score,
                        f"Sentiment filter: {idx.score:+.2f} bearish for {idx.coin} "
                        f"(n={idx.sample_size})", signal.price, signal.atr,
                        signal.stop_price,
                    )
                    self.audit.record(AuditEvent.SIGNAL, {
                        "event": "sentiment_block_buy", "coin": idx.coin,
                        "score": idx.score, "sample_size": idx.sample_size,
                    })
                elif in_position and self.sentiment.should_force_exit(sym):
                    signal = Signal(
                        Action.SELL, 95,
                        f"Sentiment collapse: {idx.score:+.2f} for {idx.coin} "
                        f"(n={idx.sample_size})", signal.price, signal.atr,
                    )
                    self.audit.record(AuditEvent.SIGNAL, {
                        "event": "sentiment_force_sell", "coin": idx.coin,
                        "score": idx.score, "sample_size": idx.sample_size,
                    })

            # Gate 5.6 — ADX chop sit-out + fee min-edge (HARD for new BUYs).
            if signal.action is Action.BUY:
                signal = self._apply_adx_and_fee_gates(signal, bars)

            # Gate 5.65 — breakout concurrent / per-symbol caps.
            if signal.action is Action.BUY and self._is_breakout_sleeve():
                max_n = int(getattr(self.settings, "max_concurrent_positions", 3) or 3)
                open_n = self._open_bot_position_count()
                already = self._bot_qty.get(self.settings.symbol, 0.0) > 1e-9
                if already:
                    signal = Signal(
                        Action.WAIT, signal.score,
                        f"Breakout gate: already in {self.settings.symbol} (max 1/symbol)",
                        signal.price, signal.atr, signal.stop_price,
                    )
                elif open_n >= max_n:
                    signal = Signal(
                        Action.WAIT, signal.score,
                        f"Breakout gate: max concurrent positions ({max_n}) reached",
                        signal.price, signal.atr, signal.stop_price,
                    )

            # Gate 5.66 — adaptive learner: bench negative-expectancy symbol /
            # regime after >= learner_min_sample closed trades, shrink size on
            # weak/unproven names. Exits are never gated.
            self._learner_size_mult = 1.0
            self._entry_regime = self._adx_regime(bars)
            if (signal.action is Action.BUY and self.learner.enabled
                    and getattr(self.settings, "learner_gate_enabled", True)):
                verdict = self.learner.gate(self.settings.symbol, self._entry_regime)
                gates["learner"] = verdict.to_dict()
                self.audit.record(AuditEvent.SIGNAL, {"event": "learner_gate", **verdict.to_dict()})
                if not verdict.allow:
                    signal = Signal(
                        Action.WAIT, signal.score,
                        f"Learner bench {verdict.key}: {verdict.reason}",
                        signal.price, signal.atr, signal.stop_price,
                    )
                else:
                    self._learner_size_mult = float(verdict.size_mult)

            # Gate 5.7 — DCA accumulator sleeve (complementary to MR).
            # If the main strategy is not entering this bar and DCA is due for its
            # configured coin (default PUMP/USD), build a fixed-USD BUY signal that
            # still flows through Gate 6 (risk) and the shared execution path.
            if self.settings.dca_enabled and signal.action is not Action.BUY:
                try:
                    dca_sym = self.settings.dca_symbol
                    # Guard: only size the DCA buy if the configured coin can
                    # actually be ordered at this account size. A coin below
                    # Kraken's lot minimum (e.g. PUMP/USD at this balance) would
                    # otherwise raise PrecisionError mid-execution and crash the
                    # cycle. Skip silently when it cannot be sized.
                    if not self._can_size(dca_sym, self.settings.dca_fixed_usd):
                        self.audit.record(AuditEvent.SIGNAL, {
                            "event": "dca_skip", "symbol": dca_sym,
                            "reason": "below Kraken minimum order for this account size",
                        })
                    else:
                        pump_price = self.gateway.get_ticker_for(dca_sym)["last"]
                        dca_signal = self.dca.maybe_signal(
                            self._dca_state,
                            price=pump_price,
                            in_position=in_position,
                            mr_action_is_buy=(signal.action is Action.BUY),
                        )
                        if dca_signal is not None:
                            # Point this cycle's execution at the DCA coin so the
                            # idempotency key, sizing, and order target it.
                            # Restore uses the cycle-start snapshot (set at the top of
                            # run_cycle) so in-cycle rotation is not disturbed.
                            self.settings.symbol = dca_sym
                            signal = dca_signal
                            self._dca_executed_this_cycle = True
                            # DCA sizes by its own fixed USD, NOT the risk-manager
                            # notional (which would be risk_per_trade*equity and fall
                            # below Kraken's minimum order size on a small account).
                            self._dca_notional = self.settings.dca_fixed_usd
                            self.audit.record(AuditEvent.SIGNAL, {
                                "event": "dca_signal", "symbol": dca_sym,
                                "reason": dca_signal.reason, "price": pump_price,
                            })
                except BrokerError as exc:
                    self.audit.record(AuditEvent.BROKER_ERROR,
                                      {"operation": "dca_ticker", "error": str(exc)},
                                      severity="error")
        else:
            # Rotation exit: the held bot-owned lot is sold this cycle. The
            # SELL branch below requires in_position; since we only rotate a
            # coin the bot actually owns (self._bot_qty), in_position is True.
            in_position = self._bot_qty.get(self.settings.symbol, 0.0) > 1e-9 or \
                self._bot_qty.get(self._cycle_symbol_snapshot, 0.0) > 1e-9

        # Gate 6 — risk
        equity = self._account_equity()
        state = self.state_store.load(equity)
        state.current_equity = equity
        state.peak_equity = max(state.peak_equity, equity)
        pnl_synced = self._refresh_live_realized_pnl(state)
        prior_realized_pnl = state.realized_pnl_today
        open_exposure = 0.0
        try:
            # Aggregate every bot-owned spot lot, not merely the currently
            # selected symbol. Kraken execution is permanently spot-only.
            open_exposure = self._bot_open_exposure_usd()
        except Exception:
            # Margin/positions endpoint errors must never block trading or
            # crash the cycle — exposure is advisory (risk still caps per-trade).
            open_exposure = 0.0
        leverage = None
        returns = bars["close"].pct_change().dropna() if len(bars) > 1 else None
        risk = self.risk.evaluate(
            signal,
            state,
            open_exposure_usd=open_exposure,
            returns=pd.Series(returns) if returns is not None else None,
        )
        if signal.action is Action.BUY and not pnl_synced:
            risk = RiskDecision(False, "Closed-trade P&L ledger unavailable; entry blocked")

        # An entry requires healthy market quality; an exit must never be
        # blocked by a wide spread — being trapped in a position is worse.
        if signal.action is Action.BUY and risk.approved and not quality_ok:
            risk = RiskDecision(False, f"Market quality gate: {quality_reason}")

        self.audit.record(AuditEvent.RISK_DECISION, {
            "approved": risk.approved, "reason": risk.reason,
            "notional_usd": risk.notional_usd,
            "planned_loss_usd": risk.planned_loss_usd,
            "equity": equity, "orders_today": state.orders_today,
        })

        order_id: str | None = None
        bar_timestamp = str(bars.index[-1]) if len(bars) else datetime.now(timezone.utc).isoformat()

        # Gates 7–9 — precision, idempotency, execution
        if signal.action is Action.BUY and risk.approved:
            buy_notional = (
                self._dca_notional
                if getattr(self, "_dca_executed_this_cycle", False)
                else risk.notional_usd
            )
            # Sizing floor that actually clears the exchange. On a small account
            # the risk-per-trade notional (risk_per_trade*equity) is often
            # BELOW a coin's lot minimum, so a raw momentum BUY would raise
            # PrecisionError and crash the cycle (this is why the bot looked
            # dead). Enforce a fillable floor = the coin's real minimum order
            # notional, still bounded by the authorized exposure cap. Real
            # equity and the explicitly authorized strategy budget both bind.
            sym = self.settings.symbol
            if not getattr(self, "_dca_executed_this_cycle", False):
                mult = float(getattr(self, "_learner_size_mult", 1.0))
                if mult < 1.0:
                    gates["learner_size"] = {"mult": mult, "before": buy_notional,
                                             "after": buy_notional * mult}
                    buy_notional *= mult
            try:
                coin_min = self._min_notional(sym)
            except Exception:
                coin_min = self.settings.min_order_notional_usd
            max_affordable = (
                min(equity, self.settings.strategy_equity_usd)
                * self.settings.max_position_fraction
            )
            if buy_notional < coin_min:
                buy_notional = coin_min
            if buy_notional > max_affordable:
                buy_notional = max_affordable
            if buy_notional < coin_min or buy_notional <= 0:
                risk = RiskDecision(
                    False,
                    f"Order size below exchange minimum for {sym} "
                    f"(balance too small to trade this coin)",
                )
                order_id = None
            else:
                order_id, risk = self._execute_buy(
                    notional=buy_notional, risk=risk,
                    bar_timestamp=bar_timestamp, state=state, gates=gates,
                    leverage=leverage, dca=getattr(self, "_dca_executed_this_cycle", False),
                    signal_price=signal.price,
                    signal_atr=signal.atr,
                    signal_stop=signal.stop_price,
                )
            # If this BUY was a DCA accumulator entry and an order was actually
            # placed, update its counters. Symbol restoration happens below
            # (outside this branch) so it runs even if risk rejected the order.
            if getattr(self, "_dca_executed_this_cycle", False) and order_id is not None:
                self.dca.record_buy(self._dca_state)
            elif order_id is not None:
                try:
                    self.learner.note_entry(sym, regime=self._entry_regime, notional=buy_notional)
                except Exception:
                    pass
        elif signal.action is Action.SELL and in_position and risk.approved:
            # Exits use the risk manager's verdict (approved for SELL, never
            # blocked by entry breakers). Cancel any attached bracket orders
            # first so the exchange-native stop/target don't fight the close.
            if self._cancel_attached_bracket(state):
                order_id, risk = self._execute(
                    side="sell", notional=0.0, risk=risk,
                    bar_timestamp=bar_timestamp, state=state, gates=gates,
                    leverage=leverage, signal_price=signal.price,
                )
            else:
                order_id = None
                risk = RiskDecision(False, "Exit blocked: owned bracket cancellation unconfirmed")

        # Restore the cycle's primary symbol if the DCA sleeve had swapped it.
        # Uses the cycle-start snapshot so in-cycle rotation is preserved and a
        # rejected DCA order does NOT leave the engine pinned to PUMP/USD.
        if getattr(self, "_dca_executed_this_cycle", False):
            self.settings.symbol = self._cycle_symbol_snapshot
            self._dca_executed_this_cycle = False

        # After a rotation exit, point the engine at the stronger coin so the
        # NEXT cycle enters it (this cycle only sold the held bot-owned lot).
        if rotation_target is not None:
            self.settings.symbol = rotation_target

        # Adapt risk scaling from the session's realized P&L streak. In live
        # mode the per-cycle equity delta equals the realized P&L of any trade
        # closed this cycle; in paper mode the per-trade update already ran in
        # the SELL path. Either way the win/loss streak drives the scale (#6).
        if (
            not (self.settings.paper_trading or self.settings.dry_run)
            and signal.action is Action.SELL
            and order_id is not None
            and self._refresh_live_realized_pnl(state)
        ):
            cycle_pnl = state.realized_pnl_today - prior_realized_pnl
        else:
            cycle_pnl = state.current_equity - equity
        if abs(cycle_pnl) > 1e-9:
            self.risk.update_scale_from_trade(cycle_pnl, state)
        else:
            self.risk.update_scale(state)
        self.state_store.save(state)
        record = DecisionRecord(
            symbol=self.settings.symbol,
            signal=signal,
            risk=risk,
            dry_run=self.settings.dry_run,
            order_id=order_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self.journal.append(record)
        return CycleResult(record, None, None, gates)

    def _execute(self, *, side: str, notional: float, risk: RiskDecision,
                 bar_timestamp: str, state, gates: dict, leverage: float | None = None,
                 bracket_plan=None,
                 signal_price: float = 0.0) -> tuple[str | None, RiskDecision]:
        """Reserve an idempotency key, then execute. Never resends on ambiguity."""
        key = make_intent_key(
            symbol=self.settings.symbol, side=side,
            notional_usd=notional, bar_timestamp=bar_timestamp,
        )
        try:
            record = self.ledger.reserve(
                key=key, symbol=self.settings.symbol, side=side,
                notional_usd=notional, bar_timestamp=bar_timestamp,
                dry_run=self.settings.dry_run,
            )
        except DuplicateOrderError as exc:
            self.audit.record(AuditEvent.ORDER_DUPLICATE_BLOCKED,
                              {"key": key, "error": str(exc)}, severity="warning")
            gates["idempotency"] = {"blocked": True, "reason": str(exc)}
            return None, RiskDecision(False, f"Duplicate order blocked: {exc}")

        gates["idempotency"] = {"blocked": False, "key": key, "userref": record.userref}
        try:
            if side == "buy":
                if bracket_plan is not None:
                    # Advanced path: submit the bracket/limit order via AddOrder.
                    bracket_plan.userref = record.userref
                    if not (self.settings.paper_trading or self.settings.dry_run):
                        # Persist before the exchange call. If submission is
                        # ambiguous or the process crashes, the next engine
                        # rebuild blocks its exit until the bracket is manually
                        # reconciled instead of selling against a reserved stop.
                        self._record_bracket_ref(
                            self.settings.symbol, "", [], complete=False
                        )
                    params = bracket_plan.to_addorder_params()
                    order_id = self.gateway.add_order(params, signal_price=signal_price)
                    if self.settings.paper_trading or self.settings.dry_run:
                        ticker = self.gateway.get_ticker()
                        sized = self.gateway.size_buy(notional, price=float(ticker["ask"]))
                        fill = self.fill_model.buy(
                            price=float(sized.price),
                            volume=float(sized.volume),
                            bid=float(ticker.get("bid", 0.0)) if ticker.get("bid") else None,
                            ask=float(ticker.get("ask", 0.0)) if ticker.get("ask") else None,
                        )
                        self.paper_portfolio.record_buy(
                            symbol=self.settings.symbol,
                            quantity=float(sized.volume),
                            fill_price=fill.price,
                            fee=fill.fee,
                            when=datetime.now(timezone.utc).isoformat(),
                        )
                        portfolio = self.paper_portfolio.snapshot()
                        state.current_equity = portfolio.equity
                        state.peak_equity = max(state.peak_equity, state.current_equity)
                else:
                    order_id = self.gateway.buy_notional(
                        notional, userref=record.userref,
                        signal_price=signal_price,
                    )
                    if order_id is not None:
                        # Legacy / DCA buy path: record the bot-owned lot so a
                        # later SELL never sweeps external holdings (#3).
                        live_fill = getattr(self.gateway, "last_fill", None)
                        sized_qty = (
                            float(live_fill.volume)
                            if live_fill is not None and live_fill.order_id == order_id
                            else float(self.gateway.size_buy(
                                notional,
                                price=float(self.gateway.get_ticker().get("ask", 0) or 1),
                            ).volume)
                        )
                        self._record_bot_buy(self.settings.symbol, sized_qty)
                    if self.settings.paper_trading or self.settings.dry_run:
                        ticker = self.gateway.get_ticker()
                        sized = self.gateway.size_buy(notional, price=float(ticker["ask"]))
                        fill = self.fill_model.buy(
                            price=float(sized.price),
                            volume=float(sized.volume),
                            bid=float(ticker.get("bid", 0.0)) if ticker.get("bid") else None,
                            ask=float(ticker.get("ask", 0.0)) if ticker.get("ask") else None,
                        )
                        self.paper_portfolio.record_buy(
                            symbol=self.settings.symbol,
                            quantity=float(sized.volume),
                            fill_price=fill.price,
                            fee=fill.fee,
                            when=datetime.now(timezone.utc).isoformat(),
                        )
                        portfolio = self.paper_portfolio.snapshot()
                        state.current_equity = portfolio.equity
                        state.peak_equity = max(state.peak_equity, state.current_equity)
            else:
                # No-sweep guard (audit finding #3, hardened): only ever sell a
                # lot the bot itself recorded buying. If the ledger has no
                # bot-owned quantity for this symbol, abort the sell — never
                # call close_position with quantity=None (which would liquidate
                # the ENTIRE exchange balance, including external/legacy holdings).
                bot_qty = self._bot_qty.get(self.settings.symbol, 0.0)
                if bot_qty <= 1e-9:
                    self.ledger.fail(key, "no-sweep: no bot-owned lot to sell")
                    self.audit.record(AuditEvent.ORDER_REJECTED,
                                      {"key": key, "reason": "no-sweep: no bot-owned lot",
                                       "gate": "no_sweep"}, severity="warning")
                    return None, RiskDecision(False, "No bot-owned lot to sell (sweep blocked)")
                order_id = self.gateway.close_position(
                    userref=record.userref, quantity=bot_qty,
                    signal_price=signal_price,
                )
                live_fill = getattr(self.gateway, "last_fill", None)
                sold_qty = (
                    float(live_fill.volume)
                    if live_fill is not None and live_fill.order_id == order_id
                    else bot_qty
                )
                self._record_bot_sell(self.settings.symbol, sold_qty)
                if self.settings.paper_trading or self.settings.dry_run:
                    ticker = self.gateway.get_ticker()
                    portfolio = self.paper_portfolio.snapshot()
                    position = portfolio.positions.get(self.settings.symbol)
                    quantity = float(position.quantity) if position else 0.0
                    cost_basis = (
                        quantity * float(position.entry_price) + float(position.fees_paid)
                        if position else 0.0
                    )
                    if quantity > 1e-12:
                        fill = self.fill_model.sell(
                            price=float(ticker["last"]),
                            volume=quantity,
                            bid=float(ticker.get("bid", 0.0)) if ticker.get("bid") else None,
                            ask=float(ticker.get("ask", 0.0)) if ticker.get("ask") else None,
                        )
                        realized = self.paper_portfolio.record_sell(
                            symbol=self.settings.symbol,
                            quantity=quantity,
                            fill_price=fill.price,
                            fee=fill.fee,
                            when=datetime.now(timezone.utc).isoformat(),
                        )
                        # Self-learning: feed the closed-trade outcome back in.
                        entry_meta = self.learner.pop_entry(self.settings.symbol) or {}
                        self.learner.record_trade(
                            self.settings.symbol, realized,
                            entry_meta.get("regime") or self.learner.last_regime,
                            notional=cost_basis or entry_meta.get("notional"),
                            strategy=entry_meta.get("strategy"),
                        )
                        # Adaptive risk: update win/loss streak + scale from
                        # the realized P&L of this closed trade (audit #6).
                        self.risk.update_scale_from_trade(realized, state)
                        state.realized_pnl_today += realized
                        state.current_equity = self.paper_portfolio.snapshot().equity
                        state.peak_equity = max(state.peak_equity, state.current_equity)
        except PrecisionError as exc:
            self.ledger.fail(key, f"precision: {exc}")
            self.audit.record(AuditEvent.ORDER_REJECTED,
                              {"key": key, "reason": str(exc), "gate": "precision"},
                              severity="warning")
            return None, RiskDecision(False, f"Precision gate: {exc}")
        except (BrokerError, SafetyLockError) as exc:
            self.ledger.fail(key, str(exc))
            self.audit.record(AuditEvent.ORDER_REJECTED,
                              {"key": key, "reason": str(exc), "gate": "execution"},
                              severity="error")
            return None, RiskDecision(False, f"Execution blocked: {exc}")

        self.ledger.confirm(key, order_id)
        state.orders_today += 1
        state.last_order_at = datetime.now(timezone.utc)
        return order_id, risk

    def _execute_buy(self, *, notional: float, risk: RiskDecision,
                     bar_timestamp: str, state, gates: dict,
                     leverage: float | None, dca: bool,
                     signal_price: float,
                     signal_atr: float | None = None,
                     signal_stop: float | None = None) -> tuple[str | None, RiskDecision]:
        """Advanced BUY path: bracket (SL+TP) and/or limit entry.

        Falls back to the legacy ``buy_notional`` market order when neither
        bracket nor limit is configured. All other gates (idempotency,
        precision, risk) are still enforced by ``_execute``.
        """
        from .orders import BracketPlan, limit_entry_price, fmt_price, build_take_profit_order

        s = self.settings
        # Reference price for SL/TP math and limit posting.
        ticker = self.gateway.get_ticker_for(s.symbol) if hasattr(self.gateway, "get_ticker_for") else self.gateway.get_ticker()
        touch = float(ticker.get("ask", 0.0) or ticker.get("last", 0.0))
        if touch <= 0:
            return self._execute(side="buy", notional=notional, risk=risk,
                                 bar_timestamp=bar_timestamp, state=state, gates=gates,
                                 leverage=leverage, signal_price=signal_price)
        meta = self.gateway.resolve_symbol()
        sized = self.gateway.size_buy(notional, price=touch)
        volume = sized.volume_str

        use_bracket = s.use_bracket and not dca  # DCA keeps it simple (no bracket)
        use_limit = (s.order_type == "limit") and not dca
        if not (use_bracket or use_limit):
            return self._execute(side="buy", notional=notional, risk=risk,
                                 bar_timestamp=bar_timestamp, state=state, gates=gates,
                                 leverage=leverage, signal_price=signal_price)

        entry_price = None
        if use_limit:
            # Round to the pair's price precision or Kraken rejects the order
            # (BTC/USD allows 1 decimal). A rounded price that lands on or
            # through the touch would fill as a taker, so nudge it one tick
            # further inside the spread to keep the maker fee.
            entry_price = fmt_price(
                limit_entry_price("buy", touch, s.limit_offset_pct,
                                  meta.pair_decimals),
                meta.pair_decimals,
            )
            if float(entry_price) >= touch:
                tick = 10.0 ** (-int(meta.pair_decimals))
                entry_price = fmt_price(
                    max(0.0, float(entry_price) - tick), meta.pair_decimals
                )
        sl = None
        if use_bracket:
            # Prefer ATR-scaled stop when signal ATR is present; else pct.
            # Distance is clamped vs stop_loss_pct (see atr_scaled_stop).
            from .orders import atr_scaled_stop
            entry_px = float(entry_price or touch)
            atr_val = signal_atr
            if atr_val is None and signal_stop is not None and signal_stop > 0 and entry_px > 0:
                atr_val = abs(entry_px - float(signal_stop)) / max(s.atr_stop_multiplier, 1e-9)
            sl, stop_src = atr_scaled_stop(
                "buy",
                entry_px,
                atr=atr_val,
                atr_multiplier=s.atr_stop_multiplier,
                stop_loss_pct=s.stop_loss_pct,
            )
            gates.setdefault("bracket", {})["stop_source"] = stop_src
            gates["bracket"]["stop_loss"] = sl

        plan = BracketPlan(
            pair=meta.key,
            side="buy",
            volume=volume,
            ordertype="limit" if use_limit else "market",
            entry_price=entry_price,
            stop_loss=sl,
            userref=None,  # filled by _execute idempotency
            trailing=s.trailing_stop,
            pair_decimals=meta.pair_decimals,
        )
        # Route through _execute so idempotency/ledger/precision still apply,
        # but submit via the advanced add_order with the bracket block.
        res = self._execute(
            side="buy", notional=notional, risk=risk,
            bar_timestamp=bar_timestamp, state=state, gates=gates,
            leverage=leverage, bracket_plan=plan, signal_price=signal_price,
        )
        bracket_child_ids: list[str] = []
        native_stop_id: str | None = None
        take_profit_id: str | None = None
        if use_bracket and res[0] is not None and not (s.paper_trading or s.dry_run):
            query_order = getattr(self.gateway, "query_order", None)
            if callable(query_order):
                try:
                    entry = query_order(res[0])
                    if isinstance(entry, dict):
                        stop_id = str(entry.get("closetxid", ""))
                        if stop_id:
                            bracket_child_ids.append(stop_id)
                            native_stop_id = stop_id
                except (BrokerError, SafetyLockError, ValueError):
                    pass
        # Take-profit: a separate resting limit order (avoids close[1] which
        # this tier rejects). Only on a real entry with bracket enabled.
        if use_bracket and s.take_profit_pct > 0 and res[0] is not None:
            try:
                tp_price = (float(entry_price or touch)) * (1.0 + s.take_profit_pct)
                tp_params = build_take_profit_order(
                    meta.key, "buy", volume, tp_price,
                    userref=(plan.userref or 0) + 2 if plan.userref else None,
                    pair_decimals=meta.pair_decimals,
                )
                take_profit_id = self.gateway.add_order(tp_params)
                if not (s.paper_trading or s.dry_run) and take_profit_id:
                    bracket_child_ids.append(str(take_profit_id))
            except (BrokerError, SafetyLockError):
                pass
        if res[0] is not None:
            self._record_bot_buy(s.symbol, float(volume))
            if use_bracket and plan.userref is not None:
                # An empty child list is deliberately persisted too: a future
                # exit must fail closed instead of risking a SELL against a
                # stop whose exact exchange ID could not be reconciled.
                bracket_complete = native_stop_id is not None and (
                    s.take_profit_pct <= 0 or take_profit_id is not None
                )
                self._record_bracket_ref(
                    s.symbol, str(res[0]), bracket_child_ids, complete=bracket_complete
                )
        return res

    # ── bot-owned lot ledger (audit finding #3) ─────────────
    def _hydrate_bot_qty_from_paper_portfolio(self) -> None:
        """Copy open paper portfolio lots into the paper bot-qty ledger.

        The paper loop constructs a new TradingEngine every cycle. Without a
        persisted paper ledger (or after upgrading from a build that NO-OPed
        saves), ``_bot_qty`` starts empty even though ``paper_portfolio.json``
        still holds open lots — so ``in_position`` stays False and exits never
        fire. Hydrate once on init when the ledger is empty.
        """
        equity = float(getattr(self.settings, "strategy_equity_usd", 0.0) or 0.0)
        try:
            self.paper_portfolio.load(equity=equity, cash=equity)
        except Exception:
            return
        snap = self.paper_portfolio.snapshot()
        holdings = {
            str(sym): float(pos.quantity)
            for sym, pos in snap.positions.items()
            if float(pos.quantity) > 1e-9
        }
        if not holdings:
            return
        self._bot_qty.update(holdings)
        self._save_bot_qty()

    def _apply_paper_protective_exit(self, signal: Signal, bars) -> Signal:
        """Force SELL when paper entry vs last price hits stop or take-profit."""
        sym = self.settings.symbol
        try:
            snap = self.paper_portfolio.snapshot()
            pos = snap.positions.get(sym)
        except Exception:
            return signal
        if pos is None or float(pos.quantity) <= 1e-9 or float(pos.entry_price) <= 0:
            return signal
        entry = float(pos.entry_price)
        last: float | None = None
        try:
            if bars is not None and len(bars) and "close" in bars:
                last = float(bars["close"].iloc[-1])
        except Exception:
            last = None
        if last is None:
            try:
                ticker = self.gateway.get_ticker()
                last = float(ticker["last"])
            except Exception:
                return signal
        stop_pct = float(self.settings.stop_loss_pct)
        tp_pct = float(self.settings.take_profit_pct)
        if last <= entry * (1.0 - stop_pct):
            return Signal(
                Action.SELL,
                99,
                f"Paper stop loss hit: last={last:.8g} entry={entry:.8g} "
                f"stop={stop_pct:.2%}",
                last,
                signal.atr,
                signal.stop_price,
            )
        if last >= entry * (1.0 + tp_pct):
            return Signal(
                Action.SELL,
                99,
                f"Paper take profit hit: last={last:.8g} entry={entry:.8g} "
                f"tp={tp_pct:.2%}",
                last,
                signal.atr,
                signal.stop_price,
            )
        return signal

    def _load_bot_qty(self) -> dict[str, float]:
        try:
            import json
            return json.loads(self._bot_qty_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _reconcile_bot_qty(self) -> None:
        """Drop bot-owned lots the exchange no longer holds.

        ``logs/bot_positions.json`` is the sole source of truth for
        ``in_position``. If a lot is closed outside the bot (manual sale,
        bracket stop, exchange-side liquidation) the file keeps claiming the
        bot owns it, so the strategy is called with ``in_position=True``
        forever and returns WAIT — the "stale-phantom-lot freeze": the bot
        looks alive, cycles every interval, and never trades again.

        Reconciliation compares each persisted lot against the live balance
        for that pair's base asset and clears any lot the exchange reports as
        zero. A live read failure is NOT treated as "position closed" — that
        would silently discard real lots. It leaves the ledger untouched and
        lets the next cycle retry.
        """
        if self.settings.paper_trading or self.settings.dry_run:
            return
        if not self._bot_qty:
            return
        try:
            balances = self.gateway.balances()
        except Exception:
            # Cannot verify -> keep the persisted lots. Failing closed here
            # means an unverified read never deletes a real position.
            return
        if not balances:
            return

        cleared: list[dict[str, object]] = []
        for symbol in list(self._bot_qty):
            try:
                base = self.gateway.resolve_symbol(symbol).base
            except Exception:
                # Unknown/unresolvable pair: without a base asset we cannot
                # verify the lot, so leave it alone rather than guess.
                continue
            held = float(balances.get(base, 0.0) or 0.0)
            if held > 0.0:
                continue
            qty = self._bot_qty.pop(symbol, None)
            cleared.append({"symbol": symbol, "asset": base, "qty": qty})

        if not cleared:
            return
        self._save_bot_qty()
        self.audit.record(
            AuditEvent.SIGNAL,
            {
                "event": "phantom_lot_reconciled",
                "cleared": cleared,
                "reason": "exchange balance is zero for a bot-owned lot",
            },
        )

    def _reconcile_bracket_refs(self) -> None:
        """Recover missing bracket child txids for lots the bot still owns.

        When a bracket's child IDs were never captured (crash between submit
        and persist, an older build, or ``closetxid`` missing from the query)
        the record stays incomplete and every later exit fails closed — the
        bot can never sell its own lot. This rediscovers the IDs by scanning
        the open book for orders whose **userref** matches the one the
        idempotency ledger recorded for this entry.

        Two independent guards prevent adopting a foreign order: the userref
        must match the bot's own entry intent, AND the order's pair must match
        the bracket's symbol. A lookup failure leaves the record untouched.
        """
        if self.settings.paper_trading or self.settings.dry_run:
            return
        if not self._bracket_refs or not self._bot_qty:
            return
        find_children = getattr(self.gateway, "find_children_by_userref", None)
        if not callable(find_children):
            return

        for symbol, ref in list(self._bracket_refs.items()):
            if ref.get("complete"):
                continue
            if float(self._bot_qty.get(symbol, 0.0)) <= 0.0:
                continue
            entry_id = str(ref.get("entry_order_id") or "")
            if not entry_id:
                continue
            # userref is derived from the intent key, not the order id, so it
            # can only be recovered from the ledger that recorded the intent.
            userref = self._userref_for_order(entry_id)
            if userref is None:
                continue
            try:
                children = find_children(userref)
                pair = self.gateway.resolve_symbol(symbol).key
            except Exception:
                continue
            if not children:
                continue
            # Kraken's OpenOrders reports the pair as the *altname* (XBTUSD)
            # while resolve_symbol() returns the canonical key (XXBTZUSD), so a
            # raw string compare would reject the bot's own stop. Normalize
            # both sides before matching — and never match on a bare substring.
            known = {str(x) for x in (ref.get("child_order_ids") or [])}
            expected_pairs = {_norm_pair(pair)}
            try:
                expected_pairs.add(_norm_pair(
                    self.gateway.resolve_symbol(symbol).altname
                ))
            except Exception:
                pass
            existing_ids = ref.get("child_order_ids")
            merged: list[str] = [str(x) for x in existing_ids] if isinstance(existing_ids, list) else []
            recovered: list[str] = []
            for order in children:
                # Both checks required: never absorb another order that merely
                # shares a symbol or happens to reuse a userref.
                if _norm_pair(str(order.get("symbol", ""))) not in expected_pairs:
                    continue
                order_id = str(order.get("id", ""))
                if order_id and order_id not in known:
                    recovered.append(order_id)
            if not recovered:
                continue
            ref["child_order_ids"] = merged + recovered
            ref["complete"] = True
            self._save_bracket_refs()
            self.audit.record(
                AuditEvent.SIGNAL,
                {
                    "event": "bracket_children_recovered",
                    "symbol": symbol,
                    "entry_order_id": entry_id,
                    "recovered": recovered,
                },
            )

    def _userref_for_order(self, order_id: str) -> int | None:
        """Userref the ledger recorded for ``order_id``, if any."""
        records = getattr(getattr(self, "ledger", None), "_records", None)
        if not isinstance(records, dict):
            return None
        for record in records.values():
            if str(getattr(record, "order_id", "")) == order_id:
                value = getattr(record, "userref", None)
                if isinstance(value, int):
                    return value
        return None

    def _save_bot_qty(self) -> None:
        # Paper/dry-run persist to logs/paper_bot_positions.json (path set in
        # __init__). Live uses logs/bot_positions.json. Live must never read
        # the paper file — keeping the ledgers separate prevents a paper buy
        # from freezing the live bot into exit-only mode.
        self._bot_qty_path.parent.mkdir(parents=True, exist_ok=True)
        self._bot_qty_path.write_text(json.dumps(self._bot_qty), encoding="utf-8")

    def _record_bot_buy(self, symbol: str, qty: float) -> None:
        self._bot_qty[symbol] = self._bot_qty.get(symbol, 0.0) + qty
        self._save_bot_qty()

    def _record_bot_sell(self, symbol: str, qty: float | None = None) -> None:
        if qty is None:
            # Full bot lot sold.
            self._bot_qty.pop(symbol, None)
        else:
            remaining = self._bot_qty.get(symbol, 0.0) - qty
            if remaining <= 1e-12:
                self._bot_qty.pop(symbol, None)
            else:
                self._bot_qty[symbol] = remaining
        self._save_bot_qty()

    def _load_bracket_refs(self) -> dict[str, dict[str, object]]:
        try:
            data = json.loads(self._bracket_refs_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {}
            records: dict[str, dict[str, object]] = {}
            for symbol, value in data.items():
                if not isinstance(value, dict):
                    # Old userref-only records cannot prove order ownership.
                    records[str(symbol)] = {"child_order_ids": []}
                    continue
                entry_id = value.get("entry_order_id")
                child_ids = value.get("child_order_ids")
                if not isinstance(entry_id, str) or not isinstance(child_ids, list):
                    records[str(symbol)] = {"child_order_ids": []}
                    continue
                records[str(symbol)] = {
                    "entry_order_id": entry_id,
                    "child_order_ids": [str(order_id) for order_id in child_ids if str(order_id)],
                    "complete": bool(value.get("complete", False)),
                }
            return records
        except (OSError, ValueError, TypeError):
            return {}

    def _save_bracket_refs(self) -> None:
        if self.settings.paper_trading or self.settings.dry_run:
            return
        self._bracket_refs_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            dir=str(self._bracket_refs_path.parent), prefix=".bot_brackets.", suffix=".tmp"
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._bracket_refs, handle)
            os.replace(temporary, self._bracket_refs_path)
        except OSError:
            if os.path.exists(temporary):
                os.unlink(temporary)
            raise

    def _record_bracket_ref(
        self, symbol: str, entry_order_id: str, child_order_ids: list[str], *, complete: bool = True
    ) -> None:
        next_record = {
            "entry_order_id": entry_order_id,
            "child_order_ids": list(child_order_ids),
            "complete": complete,
        }
        prior = self._bracket_refs.get(symbol)
        self._bracket_refs[symbol] = next_record
        try:
            self._save_bracket_refs()
        except OSError:
            if prior is None:
                self._bracket_refs.pop(symbol, None)
            else:
                self._bracket_refs[symbol] = prior
            raise

    def _bracket_ref_for(self, symbol: str) -> dict[str, object] | None:
        return self._bracket_refs.get(symbol)

    def _clear_bracket_ref(self, symbol: str) -> None:
        self._bracket_refs.pop(symbol, None)
        self._save_bracket_refs()

    def _bot_open_exposure_usd(self) -> float:
        """Market value of all bot-owned spot lots across symbols."""
        total = 0.0
        for symbol, quantity in self._bot_qty.items():
            if quantity <= 0:
                continue
            try:
                price = float(self.gateway.get_ticker_for(symbol)["last"])
            except Exception:
                # Unknown exposure must fail closed. Returning infinity blocks
                # new entries until valuation is available again.
                return float("inf")
            total += quantity * price
        return total

    def live_price(self, symbol: str | None = None) -> float | None:
        """Best available price: real-time WS feed if connected, else REST.

        Read-only helper used for intraday protective-stop checks between the
        slower REST cycles.  Never touches the order path.
        """
        sym = symbol or self.settings.symbol
        if self._feed is not None:
            snap = self._feed.latest(sym)
            if snap is not None and snap.last > 0 and snap.source == "ws":
                return snap.last
        try:
            return float(self.gateway.get_ticker_for(sym)["last"])
        except Exception:
            return None

    def check_realtime_stop(self, entry_price: float, stop_price: float) -> bool:
        """True if the live price has breached the protective stop.

        Called between REST cycles in day-trade mode so a stop breach exits
        within seconds rather than waiting up to ``monitor_interval_seconds``.
        Returns False when no position / no feed / no breach.
        """
        if entry_price <= 0 or stop_price <= 0:
            return False
        if not self.gateway.has_position():
            return False
        price = self.live_price()
        if price is None:
            return False
        # Long position: stop breached when price <= stop.
        return price <= stop_price

    def _cancel_attached_bracket(self, state) -> bool:
        """Release only verified child orders before a bot-owned SELL."""
        # Paper/dry-run never leave live child stops on the book. Incomplete
        # paper bracket refs must not fail-closed and block simulated exits.
        if self.settings.paper_trading or self.settings.dry_run:
            if self._bracket_ref_for(self.settings.symbol) is not None:
                self._clear_bracket_ref(self.settings.symbol)
            return True
        record = self._bracket_ref_for(self.settings.symbol)
        if record is None:
            return True
        if record.get("complete") is not True:
            return False
        child_ids = record.get("child_order_ids")
        if not isinstance(child_ids, list) or not child_ids:
            return False
        cancel_orders = getattr(self.gateway, "cancel_orders", None)
        if not callable(cancel_orders):
            return False
        try:
            cancelled = cancel_orders([str(order_id) for order_id in child_ids])
        except (BrokerError, SafetyLockError, ValueError):
            return False
        if not cancelled:
            return False
        self._clear_bracket_ref(self.settings.symbol)
        return True

    # ── backwards-compatible entry point ─────────────────────

    def run_once(self) -> DecisionRecord:
        """Legacy API used by the CLI and dashboard.

        Returns a DecisionRecord even when a gate blocked the cycle, so callers
        that expect a record keep working; the blocking reason is surfaced as a
        HALT action rather than being silently swallowed.
        """
        result = self.run_cycle()
        if result.record is not None:
            return result.record
        from .models import Signal
        signal = Signal(Action.HALT, 0, result.block_reason or "blocked", 0.0)
        record = DecisionRecord(
            symbol=self.settings.symbol,
            signal=signal,
            risk=RiskDecision(False, f"Blocked at {result.blocked_at}"),
            dry_run=self.settings.dry_run,
            order_id=None,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self.journal.append(record)
        return record

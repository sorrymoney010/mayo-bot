"""4h mean-reversion PAPER sleeve with post-only limit entries.

Runs alongside the primary engine (``regime_trend@60m``) inside
``scripts/paper_trader_loop.py``. It is **paper accounting only**: it never
calls an order-submission method on any gateway, and it refuses to run unless
all three safety locks are engaged (paper_trading, dry_run, not
allow_live_trading). Its gateway is built with ``allow_order_submission=False``.

Lifecycle per symbol (BTC/ETH/SOL by default):

1. On a freshly CLOSED 4h bar where ``MeanReversion4hStrategy`` signals
   (RSI<=38 and close<EMA50) it rests a paper post-only limit buy 0.1% under
   the signal close. The limit is kept strictly below the ask (never crosses),
   so it would be a maker order.
2. The order is live for ``meanrev_limit_valid_bars`` (1) bar. Each loop cycle
   it fills at the limit (maker fee) only if the ticker trades at/through it
   (ask or last <= limit). When the bar ends unfilled it EXPIRES — it is never
   converted into a market order.
3. In position: 3% stop from the fill (taker, slippage), 25% take-profit
   (maker, at the TP price), or the "reverted" signal exit (RSI>=55 or close>=
   EMA50) on a bar that closed after the fill (taker, at the ticker).

Shared-book safety with the other sleeve:
* ownership + pending orders are published in ``SleeveRegistry``; a symbol
  held or pending in either sleeve is off limits to the other;
* max concurrent positions counts ALL held lots plus ALL pending orders;
* exposure cap / risk sizing / daily-loss & drawdown breakers come from the
  shared ``RiskManager`` and ``SessionState``; pending notional counts as
  exposure; resting orders reserve paper cash.
Closed trades feed the shared ``LearningAgent`` under the strategy key
``meanrev_mk@240m`` so they are scored and benched separately from
``regime@60m``.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from .audit import AuditEvent, AuditLog
from .config import Settings
from .fills import FillModel
from .market_quality import MarketGuard, MarketQuality
from .models import Action
from .telemetry import blocked_entry, sleeve_decision, trade_detail
from .paper import PaperPortfolio
from .risk import RiskManager
from .sleeve_registry import SleeveRegistry
from .state import StateStore
from .strategies.meanrev4h_strategy import MeanReversion4hStrategy

SLEEVE = "meanrev_4h"
PAPER_BOOK = Path("logs/paper_portfolio.json")
PAPER_LOTS = Path("logs/paper_bot_positions.json")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


@dataclass
class SleeveResult:
    sleeve: str
    active: bool
    reason: str = ""
    actions: list[dict] = field(default_factory=list)
    symbols: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    # entry signals that a gate blocked (learner bench, risk, ...) -> shadow log
    blocked: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"sleeve": self.sleeve, "active": self.active, "reason": self.reason,
                "actions": self.actions, "symbols": self.symbols, "errors": self.errors,
                "blocked": self.blocked}


def sleeve_active(settings: Settings) -> tuple[bool, str]:
    if not getattr(settings, "meanrev_sleeve_enabled", False):
        return False, "disabled (MEANREV_SLEEVE_ENABLED=false)"
    if not (settings.paper_trading and settings.dry_run and not settings.allow_live_trading):
        return False, "disabled: paper-only sleeve and a safety lock is off"
    return True, "active (paper only)"


class MeanRevSleeve:
    def __init__(self, settings: Settings, *, gateway=None, audit: AuditLog | None = None,
                 now_fn: Callable[[], float] = time.time) -> None:
        self.base_settings = settings
        tf = int(settings.meanrev_timeframe_minutes)
        # Private copy: its own timeframe and a mutable symbol for the gateway.
        self.settings = settings.model_copy(update={"timeframe_minutes": tf,
                                                    "strategy": SLEEVE})
        self.tf_seconds = tf * 60
        self.strategy_key = f"meanrev_mk@{tf}m"
        self.now_fn = now_fn
        self.audit = audit or AuditLog(Path(settings.audit_log_path))
        if gateway is None:
            from .engine import build_gateway
            # Paper sleeve: order submission is hard-disabled on its gateway.
            gateway = build_gateway(self.settings, audit=self.audit,
                                    allow_order_submission=False)
        self.gateway = gateway
        self.strategy = MeanReversion4hStrategy(self.settings)
        self.risk = RiskManager(self.settings)
        self.fill_model = FillModel(self.settings)
        self.market_guard = MarketGuard(max_spread_bps=settings.max_spread_bps,
                                        min_dollar_volume=settings.min_dollar_volume)
        self.state_path = Path(settings.meanrev_state_path)
        self.registry = SleeveRegistry()
        self.portfolio = PaperPortfolio(PAPER_BOOK)
        self.state_store = StateStore(Path(settings.session_state_path))
        from .learner import LearningAgent
        self.learner = LearningAgent(
            settings.learner_path,
            min_trades=settings.learner_min_trades,
            enabled=settings.learner_enabled,
            strategy_key=self.strategy_key,
            priors_path=getattr(settings, "learner_priors_path", None),
            min_sample=int(getattr(settings, "learner_min_sample", 8)),
            bench_hours=float(getattr(settings, "learner_bench_hours", 72.0)),
        )

    # ── helpers ────────────────────────────────────────────────
    def universe(self) -> list[str]:
        out: list[str] = []
        for sym in self.settings.meanrev_symbols or []:
            sym = str(sym).strip().upper()
            if sym and sym not in out:
                out.append(sym)
        allow = self.settings.universe_allowlist
        if allow:
            allowed = {x.upper() for x in allow}
            out = [s for s in out if s in allowed]
        return out

    def _load_state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data.setdefault("pending", {})
        data.setdefault("positions", {})
        data.setdefault("last_order_bar", {})
        data.setdefault("events", [])
        return data

    def _save_state(self, st: dict) -> None:
        st["events"] = st["events"][-200:]
        st["updated_at"] = _iso(self.now_fn())
        st["strategy_key"] = self.strategy_key
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2, default=str), encoding="utf-8")
        tmp.replace(self.state_path)

    @staticmethod
    def _load_lots() -> dict[str, float]:
        try:
            data = json.loads(PAPER_LOTS.read_text(encoding="utf-8"))
            return {str(k): float(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    @staticmethod
    def _save_lots(lots: dict[str, float]) -> None:
        PAPER_LOTS.parent.mkdir(parents=True, exist_ok=True)
        PAPER_LOTS.write_text(json.dumps(lots), encoding="utf-8")

    def _equity(self) -> float:
        """Paper ledger book: cash + open cost basis, seeded at STRATEGY_EQUITY_USD."""
        seed = float(self.settings.strategy_equity_usd)
        snap = self.portfolio.load(equity=seed, cash=seed)
        basis = sum(p.quantity * p.entry_price for p in snap.positions.values())
        from .book_risk import futures_equity_addon
        return max(snap.cash + basis, 0.0) + futures_equity_addon(
            getattr(self.settings, "futures_ledger_path", None))

    def _bars(self, symbol: str) -> pd.DataFrame:
        self.settings.symbol = symbol
        return self.gateway.get_bars()

    def _trail_exit(self, sym: str, pos: dict, last: float) -> bool:
        from .trailing import maintain, sleeve_trail_enabled
        if not sleeve_trail_enabled(self.settings, SLEEVE):
            return False
        atr_v = 0.0
        try:
            bars = self._bars(sym)
            from .technicals import atr as atr_fn
            if bars is not None and len(bars) and "high" in bars:
                atr_v = float(atr_fn(bars, 14).iloc[-1])
        except Exception:
            atr_v = 0.0
        opened = str(pos.get("filled_at") or pos.get("opened_at") or "")
        try:
            return maintain(self.settings, SLEEVE, sym, "long", float(pos["entry"]), last,
                            opened, atr_v) == "trail"
        except Exception:
            return False

    def _event(self, st: dict, res: SleeveResult, kind: str, symbol: str, **info) -> None:
        ev = {"ts": _iso(self.now_fn()), "event": kind, "symbol": symbol, **info}
        st["events"].append(ev)
        res.actions.append(ev)
        audit_kind = {
            "limit_placed": AuditEvent.ORDER_SUBMITTED, "limit_filled": AuditEvent.ORDER_FILLED,
            "limit_expired": AuditEvent.ORDER_CANCELLED, "limit_cancelled": AuditEvent.ORDER_CANCELLED,
            "exit": AuditEvent.ORDER_FILLED,
        }.get(kind, AuditEvent.SIGNAL)
        try:
            self.audit.record(audit_kind, {"sleeve": SLEEVE, "paper": True, **ev})
        except Exception:
            pass

    # ── main entry ─────────────────────────────────────────────
    def run_cycle(self) -> SleeveResult:
        ok, why = sleeve_active(self.base_settings)
        res = SleeveResult(SLEEVE, ok, why)
        if not ok:
            return res
        now = float(self.now_fn())
        st = self._load_state()
        lots = self._load_lots()
        equity = self._equity()
        state = self.state_store.load(equity)
        state.current_equity = equity
        state.peak_equity = max(state.peak_equity, equity)
        snap = self.portfolio.snapshot()

        # Reconcile: a sleeve position whose paper lot vanished (e.g. manual
        # flatten) is dropped so it can never block the symbol forever.
        for sym in list(st["positions"]):
            if sym not in snap.positions or snap.positions[sym].quantity <= 1e-12:
                st["positions"].pop(sym)
                self._event(st, res, "position_missing", sym,
                            note="paper lot gone from book; sleeve record dropped")

        self._manage_pending(st, res, lots, state, now)
        self._manage_positions(st, res, lots, state, now)
        self._scan_entries(st, res, lots, state, equity, now)

        from .sleeve_sync import commit_sleeve_cycle
        commit_sleeve_cycle(
            self.portfolio, st, state_path=self.state_path, lots_path=PAPER_LOTS,
            save_state=self._save_state, equity=float(self.settings.strategy_equity_usd),
        )
        self.registry.load()
        self.registry.set_sleeve(
            SLEEVE, owned=set(st["positions"]),
            pending={s: float(o["notional"]) for s, o in st["pending"].items()},
        )
        self.registry.save()
        self.state_store.save(state)
        return res

    # ── pending limit orders ───────────────────────────────────
    def _manage_pending(self, st, res, lots, state, now) -> None:
        maker_bps = self.fill_model.maker_fee_bps
        for sym in list(st["pending"]):
            order = st["pending"][sym]
            limit = float(order["limit"])
            try:
                t = self.gateway.get_ticker_for(sym)
            except Exception as exc:  # keep the order; expire on time below
                res.errors.append(f"{sym} ticker: {type(exc).__name__}: {exc}")
                t = None
            touched = t is not None and (float(t["ask"]) <= limit or float(t["last"]) <= limit)
            if now < float(order["expires_at"]) and touched:
                qty = float(order["qty"])
                fee = qty * limit * maker_bps / 1e4
                snap = self.portfolio.snapshot()
                if snap.cash + 1e-9 < qty * limit + fee:
                    st["pending"].pop(sym)
                    self._event(st, res, "limit_cancelled", sym, reason="insufficient paper cash")
                    res.symbols[sym] = "limit cancelled (insufficient paper cash)"
                    continue
                self.portfolio.record_buy(symbol=sym, quantity=qty, fill_price=limit, fee=fee,
                                          when=_iso(now))
                lots[sym] = lots.get(sym, 0.0) + qty
                sp, tp = float(self.settings.meanrev_stop_pct), float(self.settings.meanrev_take_profit_pct)
                st["positions"][sym] = {
                    "entry": limit, "qty": qty, "entry_fee": fee, "filled_at": now,
                    "stop": limit * (1 - sp), "tp": limit * (1 + tp),
                    "regime": order.get("regime", "unknown"), "signal_bar": order.get("signal_bar"),
                    "signal_px": order.get("signal_px"), "entry_maker": True,
                }
                st["pending"].pop(sym)
                try:
                    self.learner.note_entry(sym, regime=order.get("regime", "unknown"),
                                            notional=qty * limit + fee)
                except Exception:
                    pass
                state.orders_today += 1
                state.last_order_at = datetime.fromtimestamp(now, tz=timezone.utc)
                self._event(st, res, "limit_filled", sym, price=limit, qty=qty, fee=round(fee, 6),
                            maker=True)
                res.symbols[sym] = f"FILLED paper limit buy {qty:g} @ {limit:.6g} (maker)"
            elif now >= float(order["expires_at"]):
                st["pending"].pop(sym)
                self._event(st, res, "limit_expired", sym, limit=limit,
                            note="unfilled post-only limit expired; not converted to market")
                res.symbols[sym] = f"limit @ {limit:.6g} EXPIRED unfilled (no market fallback)"
            else:
                res.symbols[sym] = (f"resting paper limit buy @ {limit:.6g} until "
                                    f"{_iso(float(order['expires_at']))}")

    # ── open positions ─────────────────────────────────────────
    def _manage_positions(self, st, res, lots, state, now) -> None:
        for sym in list(st["positions"]):
            pos = st["positions"][sym]
            try:
                t = self.gateway.get_ticker_for(sym)
            except Exception as exc:
                res.errors.append(f"{sym} ticker: {type(exc).__name__}: {exc}")
                res.symbols[sym] = "holding (ticker unavailable)"
                continue
            last = float(t["last"])
            exit_reason, fill = None, None
            qty = float(pos["qty"])
            if last <= float(pos["stop"]):
                exit_reason = "stop"
                fill = self.fill_model.sell(price=last, volume=qty, bid=t.get("bid"), ask=t.get("ask"))
                price, fee = fill.price, fill.fee
            elif last >= float(pos["tp"]):
                exit_reason = "tp"
                price = float(pos["tp"])
                fee = qty * price * self.fill_model.maker_fee_bps / 1e4
            elif self._trail_exit(sym, pos, last):
                exit_reason = "trail"
                fill = self.fill_model.sell(price=last, volume=qty, bid=t.get("bid"), ask=t.get("ask"))
                price, fee = fill.price, fill.fee
            else:
                try:
                    bars = self._bars(sym)
                    closed_at = bars.index[-1].timestamp() + self.tf_seconds
                    if closed_at > float(pos["filled_at"]):
                        sig = self.strategy.evaluate(bars, in_position=True)
                        sleeve_decision(self, sym, sig, bars, in_position=True,
                                        d1_enabled=bool(self.settings.meanrev_daily_filter))
                        if sig.action is Action.SELL:
                            exit_reason = "reverted"
                        res.symbols[sym] = sig.reason[:120]
                    else:
                        res.symbols[sym] = "holding (no bar closed since fill)"
                except Exception as exc:
                    res.errors.append(f"{sym} bars: {type(exc).__name__}: {exc}")
                    res.symbols[sym] = "holding (bars unavailable)"
                if exit_reason:
                    fill = self.fill_model.sell(price=last, volume=qty, bid=t.get("bid"), ask=t.get("ask"))
                    price, fee = fill.price, fill.fee
            if not exit_reason:
                res.symbols.setdefault(sym, f"holding: last {last:.6g} stop {float(pos['stop']):.6g}")
                continue
            snap = self.portfolio.snapshot()
            book = snap.positions.get(sym)
            cost_basis = book.cost_basis if book else qty * float(pos["entry"])
            sell_qty = min(qty, book.quantity) if book else qty
            try:
                realized = self.portfolio.record_sell(symbol=sym, quantity=sell_qty, fill_price=price,
                                                      fee=fee, when=_iso(now))
            except ValueError as exc:
                if "No open paper position" not in str(exc):
                    raise
                st["positions"].pop(sym, None)
                res.symbols[sym] = "already closed by the fast exit watcher"
                continue
            remaining = lots.get(sym, 0.0) - sell_qty
            if remaining <= 1e-12:
                lots.pop(sym, None)
            else:
                lots[sym] = remaining
            st["positions"].pop(sym)
            meta = self.learner.pop_entry(sym) or {}
            try:
                try:
                    xbars = self._bars(sym)
                except Exception:
                    xbars = None
                detail = trade_detail(pos, exit_signal_px=(float(pos["tp"]) if exit_reason == "tp" else last),
                                      exit_fill_px=price, exit_fee=fee, exit_maker=(exit_reason == "tp"),
                                      reason=exit_reason, bars=xbars, now=now, qty=sell_qty,
                                      cost_basis=cost_basis)
                self.learner.record_trade(sym, realized, meta.get("regime") or pos.get("regime", "unknown"),
                                          notional=cost_basis, strategy=self.strategy_key, ts=now,
                                          detail=detail)
            except Exception:
                pass
            self.risk.update_scale_from_trade(realized, state)
            state.realized_pnl_today += realized
            state.current_equity = self._equity()
            state.peak_equity = max(state.peak_equity, state.current_equity)
            net_bps = realized / cost_basis * 1e4 if cost_basis > 0 else 0.0
            self._event(st, res, "exit", sym, reason=exit_reason, price=price, qty=sell_qty,
                        realized=round(realized, 6), net_bps=round(net_bps, 1))
            res.symbols[sym] = (f"EXIT {exit_reason} @ {price:.6g} realized ${realized:+.2f} "
                                f"({net_bps:+.0f} bps)")

    # ── new entries ────────────────────────────────────────────
    def _scan_entries(self, st, res, lots, state, equity, now) -> None:
        s = self.settings
        self.registry.load()
        mine = set(st["positions"]) | set(st["pending"])
        held_all = {k for k, v in lots.items() if float(v) > 1e-9}
        blocked = self.registry.foreign_symbols(SLEEVE) | (held_all - mine)
        max_n = int(getattr(s, "max_concurrent_positions", 3) or 3)
        for sym in self.universe():
            if sym in mine:
                continue
            if sym in blocked:
                res.symbols[sym] = f"skip: {sym} held/pending by another sleeve ({self.registry.owner_of(sym)})"
                continue
            try:
                bars = self._bars(sym)
            except Exception as exc:
                res.errors.append(f"{sym} bars: {type(exc).__name__}: {exc}")
                res.symbols[sym] = "bars unavailable"
                continue
            if bars is None or not len(bars):
                res.symbols[sym] = "no bars"
                continue
            bar_open = bars.index[-1].timestamp()
            closed_at = bar_open + self.tf_seconds
            expires_at = closed_at + int(s.meanrev_limit_valid_bars) * self.tf_seconds
            bar_iso = _iso(bar_open)
            sig = self.strategy.evaluate(bars, in_position=False)
            sleeve_decision(self, sym, sig, bars, in_position=False,
                            d1_enabled=bool(s.meanrev_daily_filter))
            if sig.action is not Action.BUY:
                res.symbols[sym] = sig.reason[:140]
                if getattr(self.strategy, "d1_blocked", False) and st["last_order_bar"].get(sym) != bar_iso:
                    res.blocked.append(blocked_entry(self, sym, sig, bars, sig.reason, gate="d1"))
                continue
            if st["last_order_bar"].get(sym) == bar_iso:
                res.symbols[sym] = f"signal bar {bar_iso} already handled"
                continue
            if now >= expires_at:
                res.symbols[sym] = "signal bar too old (order window passed)"
                continue
            placed = self._try_place(st, res, sym, sig, bars, bar_iso, expires_at,
                                     lots, state, equity, now, max_n)
            if placed:
                mine.add(sym)
            else:
                res.blocked.append(blocked_entry(self, sym, sig, bars, res.symbols.get(sym, "blocked")))

    def _try_place(self, st, res, sym, sig, bars, bar_iso, expires_at,
                   lots, state, equity, now, max_n) -> bool:
        s = self.settings
        from .indicators import fee_edge_ok
        from .orders import fmt_price

        ok, reason = fee_edge_ok(float(s.meanrev_take_profit_pct), float(s.min_edge_bps))
        if s.min_edge_gate_enabled and not ok:
            res.symbols[sym] = f"BUY blocked: {reason}"
            return False
        # Shared max-positions: every held lot (any sleeve) + every pending order.
        self.registry.load()
        held_all = {k for k, v in lots.items() if float(v) > 1e-9}
        pending_all = (set(self.registry.pending) | set(st["pending"])) - held_all
        from .book_risk import futures_position_count
        if len(held_all) + len(pending_all) + futures_position_count(
                getattr(s, "futures_ledger_path", None)) >= max_n:
            res.symbols[sym] = f"BUY blocked: max concurrent positions ({max_n}) incl. pending"
            return False
        try:
            t = self.gateway.get_ticker_for(sym)
            self.settings.symbol = sym
            q = self.gateway.market_quality() if hasattr(self.gateway, "market_quality") else None
        except Exception as exc:
            res.symbols[sym] = f"BUY blocked: ticker unavailable ({type(exc).__name__})"
            return False
        if q is not None:
            q_ok, q_reason = self.market_guard.approve(MarketQuality(
                bid=q["bid"], ask=q["ask"], recent_dollar_volume=q["recent_dollar_volume"]))
            if not q_ok:
                res.symbols[sym] = f"BUY blocked: market quality ({q_reason})"
                return False
        d = self.strategy.enrich(bars)
        regime = self.strategy.regime(d)
        size_mult = 1.0
        if self.learner.enabled and getattr(s, "learner_gate_enabled", True):
            verdict = self.learner.gate(sym, regime)
            if not verdict.allow:
                res.symbols[sym] = f"BUY blocked: learner bench {verdict.key}: {verdict.reason}"[:160]
                return False
            size_mult = float(verdict.size_mult)
        # Exposure = every paper lot at market + every resting order's notional.
        exposure = 0.0
        for lot_sym, qty in lots.items():
            if float(qty) <= 1e-9:
                continue
            try:
                exposure += float(qty) * float(self.gateway.get_ticker_for(lot_sym)["last"])
            except Exception:
                res.symbols[sym] = "BUY blocked: cannot value open lots (fail closed)"
                return False
        exposure += sum(float(o["notional"]) for o in st["pending"].values())
        exposure += sum(float(p.get("notional", 0.0)) for k, p in self.registry.pending.items()
                        if p.get("sleeve") != "meanrev_4h" and k not in st["pending"])
        from .book_risk import futures_exposure_usd
        extra = futures_exposure_usd(getattr(s, "futures_ledger_path", None))
        if extra == float("inf"):
            res.symbols[sym] = "BUY blocked: futures exposure unknown (fail closed)"
            return False
        exposure += extra
        returns = bars["close"].pct_change().dropna()
        decision = self.risk.evaluate(sig, state, open_exposure_usd=exposure,
                                      returns=returns if len(returns) else None)
        if not decision.approved:
            res.symbols[sym] = f"BUY blocked: risk: {decision.reason}"
            return False
        meta = self.gateway.resolve_symbol(sym)
        decimals = int(meta.pair_decimals)
        limit = float(fmt_price(float(sig.price), decimals))
        tick = 10.0 ** (-decimals)
        ask = float(t["ask"])
        if limit >= ask:  # post-only: never rest at/through the ask
            limit = float(fmt_price(ask - tick, decimals))
        max_affordable = min(equity, float(s.strategy_equity_usd)) * float(s.max_position_fraction)
        notional = min(float(decision.notional_usd) * size_mult, max_affordable)
        coin_min = float(meta.order_min) * limit * 1.05
        if notional < coin_min:
            if coin_min <= max_affordable:
                notional = coin_min
            else:
                res.symbols[sym] = f"BUY blocked: exchange minimum {coin_min:.2f} > cap {max_affordable:.2f}"
                return False
        try:
            self.settings.symbol = sym
            sized = self.gateway.size_buy(notional, price=limit)
            qty = float(sized.volume)
        except Exception as exc:
            res.symbols[sym] = f"BUY blocked: precision ({exc})"[:160]
            return False
        cost = qty * limit * (1 + self.fill_model.maker_fee_bps / 1e4)
        reserved = sum(float(o["qty"]) * float(o["limit"]) for o in st["pending"].values())
        if self.portfolio.snapshot().cash - reserved < cost:
            res.symbols[sym] = "BUY blocked: insufficient paper cash after reservations"
            return False
        st["pending"][sym] = {
            "limit": limit, "qty": qty, "notional": round(qty * limit, 6),
            "signal_bar": bar_iso, "placed_at": now, "expires_at": expires_at,
            "regime": regime, "size_mult": size_mult, "reason": sig.reason[:200],
            "signal_px": float(sig.price),
        }
        st["last_order_bar"][sym] = bar_iso
        self._event(st, res, "limit_placed", sym, limit=limit, qty=qty,
                    notional=round(qty * limit, 2), expires_at=_iso(expires_at),
                    regime=regime, size_mult=size_mult, post_only=True)
        res.symbols[sym] = (f"PLACED paper post-only limit buy {qty:g} @ {limit:.6g} "
                            f"(${qty * limit:.2f}, x{size_mult:g}) until {_iso(expires_at)}")
        return True

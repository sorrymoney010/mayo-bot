"""4h trend-hold PAPER sleeve (third sleeve; see ``strategies/trendhold_strategy``).

Paper accounting only: never submits exchange orders (its gateway is built
with ``allow_order_submission=False``) and refuses to run unless all safety
locks are engaged.

* Entry: on the first cycle after a 4h bar closes with the trend-hold BUY
  signal (D1 on, close > EMA100, EMA20 > EMA100) -> paper market buy at the
  ticker (taker fee + slippage via ``FillModel``) = the next bar's open.
  A signal older than one bar is skipped (never chased).
* Exit: first 4h close below EMA100 on a bar that closed after the fill ->
  paper market sell. No hard stop / TP (as tested).
* Size: ``trendhold_position_fraction`` (25%) of the paper book per coin
  (x learner size multiplier), capped by remaining room under
  ``max_exposure_fraction`` (75% in run_paper_mac.sh) and by free cash.

Cross-sleeve safety (shared with regime_trend@60m and meanrev_4h):
* one position per coin across ALL sleeves — ownership is published in
  ``SleeveRegistry`` (``logs/paper_sleeve_owners.json``); held/pending symbols
  of other sleeves are skipped;
* ``max_concurrent_positions`` (3) counts every held lot + every pending order;
* exposure counts every lot at market + every pending order;
* daily-loss / drawdown breakers and cooldown come from the shared
  ``RiskManager`` + ``SessionState``.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pandas as pd

from .audit import AuditEvent, AuditLog
from .config import Settings
from .fills import FillModel
from .market_quality import MarketGuard, MarketQuality
from .meanrev_sleeve import PAPER_BOOK, PAPER_LOTS, SleeveResult, _iso
from .models import Action, Signal
from .paper import PaperPortfolio
from .risk import RiskManager
from .sleeve_registry import SleeveRegistry
from .state import StateStore
from .strategies.trendhold_strategy import TrendHoldStrategy
from .telemetry import blocked_entry, sleeve_decision, trade_detail

SLEEVE = "trendhold_4h"
TRENDHOLD_COINS = ("BTC/USD", "ETH/USD", "SOL/USD")


def sleeve_active(settings: Settings) -> tuple[bool, str]:
    if not getattr(settings, "trendhold_sleeve_enabled", False):
        return False, "disabled (TRENDHOLD_SLEEVE_ENABLED=false)"
    if not (settings.paper_trading and settings.dry_run and not settings.allow_live_trading):
        return False, "disabled: paper-only sleeve and a safety lock is off"
    return True, "active (paper only)"


class TrendHoldSleeve:
    def __init__(self, settings: Settings, *, gateway=None, audit: AuditLog | None = None,
                 now_fn: Callable[[], float] = time.time) -> None:
        self.base_settings = settings
        tf = int(settings.trendhold_timeframe_minutes)
        self.settings = settings.model_copy(update={"timeframe_minutes": tf, "strategy": SLEEVE})
        self.tf_seconds = tf * 60
        self.strategy_key = f"trendhold@{tf}m"
        self.now_fn = now_fn
        self.audit = audit or AuditLog(Path(settings.audit_log_path))
        if gateway is None:
            from .engine import build_gateway
            gateway = build_gateway(self.settings, audit=self.audit, allow_order_submission=False)
        self.gateway = gateway
        self.strategy = TrendHoldStrategy(self.settings)
        self.risk = RiskManager(self.settings)
        self.fill_model = FillModel(self.settings)
        self.market_guard = MarketGuard(max_spread_bps=settings.max_spread_bps,
                                        min_dollar_volume=settings.min_dollar_volume)
        self.state_path = Path(settings.trendhold_state_path)
        self.registry = SleeveRegistry()
        self.portfolio = PaperPortfolio(PAPER_BOOK)
        self.state_store = StateStore(Path(settings.session_state_path))
        from .learner import LearningAgent
        self.learner = LearningAgent(
            settings.learner_path, min_trades=settings.learner_min_trades,
            enabled=settings.learner_enabled, strategy_key=self.strategy_key,
            priors_path=getattr(settings, "learner_priors_path", None),
            min_sample=int(getattr(settings, "learner_min_sample", 30)),
            bench_hours=float(getattr(settings, "learner_bench_hours", 72.0)),
        )

    # ── helpers ────────────────────────────────────────────────
    def universe(self) -> list[str]:
        out: list[str] = []
        for sym in self.settings.trendhold_symbols or []:
            sym = str(sym).strip().upper()
            if sym and sym not in out:
                out.append(sym)
        # Fixed to BTC/ETH/SOL (the tested set) independent of UNIVERSE_ALLOWLIST,
        # which is the primary engine's scan basket.
        return [s for s in out if s in TRENDHOLD_COINS]

    def _load_state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            data = {}
        for k, v in (("positions", {}), ("last_signal_bar", {}), ("events", [])):
            data.setdefault(k, v)
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
        opened = str(pos.get("filled_at") or "")
        try:
            return maintain(self.settings, SLEEVE, sym, "long", float(pos["entry"]), last,
                            opened, atr_v) == "trail"
        except Exception:
            return False

    def _close_position(self, st, res, lots, state, sym, pos, ticker, reason: str, now: float) -> None:
        qty = float(pos["qty"])
        fill = self.fill_model.sell(price=float(ticker["last"]), volume=qty,
                                    bid=ticker.get("bid"), ask=ticker.get("ask"))
        book = self.portfolio.snapshot().positions.get(sym)
        cost_basis = book.cost_basis if book else qty * float(pos["entry"])
        sell_qty = min(qty, book.quantity) if book else qty
        try:
            realized = self.portfolio.record_sell(symbol=sym, quantity=sell_qty, fill_price=fill.price,
                                                  fee=fill.fee, when=_iso(now))
        except ValueError as exc:
            if "No open paper position" not in str(exc):
                raise
            st["positions"].pop(sym, None)
            res.symbols[sym] = "already closed by the fast exit watcher"
            return
        remaining = lots.get(sym, 0.0) - sell_qty
        if remaining <= 1e-12:
            lots.pop(sym, None)
        else:
            lots[sym] = remaining
        st["positions"].pop(sym)
        try:
            meta = self.learner.pop_entry(sym) or {}
            self.learner.record_trade(sym, realized, meta.get("regime") or "trend",
                                      notional=cost_basis, strategy=self.strategy_key, ts=now,
                                      detail={"exit_reason": reason})
        except Exception:
            pass
        from .state import record_close_pnl
        record_close_pnl(self.state_store.path, realized, self._equity(), self.settings, state)
        net_bps = realized / cost_basis * 1e4 if cost_basis > 0 else 0.0
        self._event(st, res, "exit", sym, reason=reason, price=fill.price, qty=sell_qty,
                    realized=round(realized, 6), net_bps=round(net_bps, 1))
        res.symbols[sym] = f"EXIT {reason} @ {fill.price:.6g} realized ${realized:+.2f} ({net_bps:+.0f} bps)"

    def _event(self, st: dict, res: SleeveResult, kind: str, symbol: str, **info) -> None:
        ev = {"ts": _iso(self.now_fn()), "event": kind, "symbol": symbol, **info}
        st["events"].append(ev)
        res.actions.append(ev)
        kind_map = {"entry": AuditEvent.ORDER_FILLED, "exit": AuditEvent.ORDER_FILLED}
        try:
            self.audit.record(kind_map.get(kind, AuditEvent.SIGNAL), {"sleeve": SLEEVE, "paper": True, **ev})
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
        for sym in list(st["positions"]):
            if sym not in snap.positions or snap.positions[sym].quantity <= 1e-12:
                st["positions"].pop(sym)
                self._event(st, res, "position_missing", sym, note="paper lot gone; sleeve record dropped")
        self._manage_positions(st, res, lots, state, now)
        self._scan_entries(st, res, lots, state, now)
        from .sleeve_sync import commit_sleeve_cycle
        commit_sleeve_cycle(
            self.portfolio, st, state_path=self.state_path, lots_path=PAPER_LOTS,
            save_state=self._save_state, equity=float(self.settings.strategy_equity_usd),
        )
        from .sleeve_registry import registry_lock
        with registry_lock(self.registry.path):
            self.registry.load()
            self.registry.set_sleeve(SLEEVE, owned=set(st["positions"]), pending={})
            self.registry.save()
        self.state_store.save(state, keep_disk_accounting=True)
        return res

    # ── open positions ─────────────────────────────────────────
    def _manage_positions(self, st, res, lots, state, now) -> None:
        for sym in list(st["positions"]):
            pos = st["positions"][sym]
            from .trailing import sleeve_trail_enabled
            if sleeve_trail_enabled(self.settings, SLEEVE):
                try:
                    t = self.gateway.get_ticker_for(sym)
                    if self._trail_exit(sym, pos, float(t["last"])):
                        self._close_position(st, res, lots, state, sym, pos, t, "trail", now)
                        continue
                except Exception:
                    pass
            try:
                bars = self._bars(sym)
                closed_at = bars.index[-1].timestamp() + self.tf_seconds
            except Exception as exc:
                res.errors.append(f"{sym} bars: {type(exc).__name__}: {exc}")
                res.symbols[sym] = "holding (bars unavailable)"
                continue
            if closed_at <= float(pos["filled_at"]):
                res.symbols[sym] = "holding (no 4h bar closed since fill)"
                continue
            sig = self.strategy.evaluate(bars, in_position=True)
            sleeve_decision(self, sym, sig, bars, in_position=True, d1_enabled=False)
            if sig.action is not Action.SELL:
                res.symbols[sym] = sig.reason[:140]
                continue
            try:
                t = self.gateway.get_ticker_for(sym)
            except Exception as exc:
                res.errors.append(f"{sym} ticker: {type(exc).__name__}: {exc}")
                res.symbols[sym] = "exit signal but ticker unavailable (retry next cycle)"
                continue
            qty = float(pos["qty"])
            fill = self.fill_model.sell(price=float(t["last"]), volume=qty, bid=t.get("bid"), ask=t.get("ask"))
            book = self.portfolio.snapshot().positions.get(sym)
            cost_basis = book.cost_basis if book else qty * float(pos["entry"])
            sell_qty = min(qty, book.quantity) if book else qty
            try:
                realized = self.portfolio.record_sell(symbol=sym, quantity=sell_qty, fill_price=fill.price,
                                                      fee=fill.fee, when=_iso(now))
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
                detail = trade_detail(pos, exit_signal_px=float(sig.price), exit_fill_px=fill.price,
                                      exit_fee=fill.fee, exit_maker=False, reason="below_ema100",
                                      bars=bars, now=now, qty=sell_qty, cost_basis=cost_basis)
                self.learner.record_trade(sym, realized, meta.get("regime") or "trend",
                                          notional=cost_basis, strategy=self.strategy_key, ts=now,
                                          detail=detail)
            except Exception:
                pass
            from .state import record_close_pnl
            record_close_pnl(self.state_store.path, realized, self._equity(), self.settings, state)
            net_bps = realized / cost_basis * 1e4 if cost_basis > 0 else 0.0
            self._event(st, res, "exit", sym, reason="below_ema100", price=fill.price, qty=sell_qty,
                        realized=round(realized, 6), net_bps=round(net_bps, 1))
            res.symbols[sym] = f"EXIT below_ema100 @ {fill.price:.6g} realized ${realized:+.2f} ({net_bps:+.0f} bps)"

    # ── new entries ────────────────────────────────────────────
    def _scan_entries(self, st, res, lots, state, now) -> None:
        self.registry.load()
        mine = set(st["positions"])
        held_all = {k for k, v in lots.items() if float(v) > 1e-9}
        blocked = self.registry.foreign_symbols(SLEEVE) | (held_all - mine)
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
            bar_iso = _iso(bar_open)
            sig = self.strategy.evaluate(bars, in_position=False)
            sleeve_decision(self, sym, sig, bars, in_position=False,
                            d1_enabled=bool(self.settings.trendhold_daily_filter))
            if sig.action is not Action.BUY:
                res.symbols[sym] = sig.reason[:160]
                if getattr(self.strategy, "d1_blocked", False):
                    res.blocked.append(blocked_entry(self, sym, sig, bars, sig.reason, gate="d1"))
                continue
            if st["last_signal_bar"].get(sym) == bar_iso:
                res.symbols[sym] = f"signal bar {bar_iso} already handled"
                continue
            if now - closed_at > self.tf_seconds:
                res.symbols[sym] = "signal bar older than one bar (not chased)"
                continue
            st["last_signal_bar"][sym] = bar_iso
            if self._try_buy(st, res, sym, sig, bar_iso, lots, state, now):
                mine.add(sym)
                held_all.add(sym)
            else:
                res.blocked.append(blocked_entry(self, sym, sig, bars, res.symbols.get(sym, "blocked")))

    def room(self, lots: dict[str, float], equity: float) -> tuple[float, float, int]:
        """(exposure_usd incl. pending, cap_usd, open_slots_used) across ALL sleeves."""
        exposure = 0.0
        for lot_sym, qty in lots.items():
            if float(qty) <= 1e-9:
                continue
            exposure += float(qty) * float(self.gateway.get_ticker_for(lot_sym)["last"])
        exposure += self.registry.pending_notional()
        from .book_risk import futures_exposure_usd, futures_position_count
        extra = futures_exposure_usd(getattr(self.settings, "futures_ledger_path", None))
        if extra == float("inf"):
            raise RuntimeError("futures exposure unknown")
        exposure += extra
        held = {k for k, v in lots.items() if float(v) > 1e-9}
        used = len(held | (set(self.registry.pending) - held))
        used += futures_position_count(getattr(self.settings, "futures_ledger_path", None))
        cap = min(equity, float(self.settings.strategy_equity_usd)) * float(self.settings.max_exposure_fraction)
        return exposure, cap, used

    def _try_buy(self, st, res, sym, sig: Signal, bar_iso, lots, state, now) -> bool:
        s = self.settings
        self.registry.load()
        max_n = int(getattr(s, "max_concurrent_positions", 3) or 3)
        equity = self._equity()
        try:
            exposure, cap, used = self.room(lots, equity)
        except Exception:
            res.symbols[sym] = "BUY blocked: cannot value open lots (fail closed)"
            return False
        if used >= max_n:
            res.symbols[sym] = f"BUY blocked: max concurrent positions ({max_n}) across all sleeves"
            return False
        try:
            t = self.gateway.get_ticker_for(sym)
            self.settings.symbol = sym
            q = self.gateway.market_quality() if hasattr(self.gateway, "market_quality") else None
        except Exception as exc:
            res.symbols[sym] = f"BUY blocked: ticker unavailable ({type(exc).__name__})"
            return False
        if q is not None:
            ok, why = self.market_guard.approve(MarketQuality(bid=q["bid"], ask=q["ask"],
                                                              recent_dollar_volume=q["recent_dollar_volume"]))
            if not ok:
                res.symbols[sym] = f"BUY blocked: market quality ({why})"
                return False
        size_mult = 1.0
        if self.learner.enabled and getattr(s, "learner_gate_enabled", True):
            verdict = self.learner.gate(sym, "trend")
            if not verdict.allow:
                res.symbols[sym] = f"BUY blocked: learner bench {verdict.key}: {verdict.reason}"[:160]
                return False
            size_mult = float(verdict.size_mult)
        price = float(t["ask"])
        # Breakers / cooldown / exposure cap from the shared RiskManager. The
        # sleeve has no stop, so a synthetic 25% distance is used only to pass
        # the stop sanity check; size below is the fixed book fraction.
        probe = Signal(Action.BUY, sig.score, sig.reason, price, sig.atr, price * 0.75)
        decision = self.risk.evaluate(probe, state, open_exposure_usd=exposure)
        if not decision.approved:
            res.symbols[sym] = f"BUY blocked: risk: {decision.reason}"
            return False
        book = min(equity, float(s.strategy_equity_usd))
        cash = self.portfolio.snapshot().cash - self.registry.pending_notional()
        notional = min(book * float(s.trendhold_position_fraction) * size_mult, cap - exposure, cash * 0.995)
        meta = self.gateway.resolve_symbol(sym)
        coin_min = float(meta.order_min) * price * 1.05
        if notional < max(coin_min, float(s.min_order_notional_usd)):
            res.symbols[sym] = f"BUY blocked: room ${notional:.2f} < minimum ${max(coin_min, 1.0):.2f}"
            return False
        try:
            sized = self.gateway.size_buy(notional, price=price)
            qty = float(sized.volume)
        except Exception as exc:
            res.symbols[sym] = f"BUY blocked: precision ({exc})"[:160]
            return False
        fill = self.fill_model.buy(price=float(t["last"]), volume=qty, bid=t.get("bid"), ask=t.get("ask"))
        if self.portfolio.snapshot().cash < qty * fill.price + fill.fee:
            res.symbols[sym] = "BUY blocked: insufficient paper cash"
            return False
        self.portfolio.record_buy(symbol=sym, quantity=qty, fill_price=fill.price, fee=fill.fee, when=_iso(now))
        lots[sym] = lots.get(sym, 0.0) + qty
        st["positions"][sym] = {"entry": fill.price, "qty": qty, "entry_fee": fill.fee, "filled_at": now,
                                "signal_bar": bar_iso, "signal_px": float(sig.price), "entry_maker": False,
                                "size_mult": size_mult, "mae_px": fill.price, "mfe_px": fill.price}
        try:
            self.learner.note_entry(sym, regime="trend", notional=qty * fill.price + fill.fee)
        except Exception:
            pass
        state.orders_today += 1
        state.last_order_at = datetime.fromtimestamp(now, tz=timezone.utc)
        self._event(st, res, "entry", sym, price=fill.price, qty=qty, fee=round(fill.fee, 6),
                    notional=round(qty * fill.price, 2), size_mult=size_mult, signal_bar=bar_iso)
        res.symbols[sym] = f"BOUGHT paper {qty:g} @ {fill.price:.6g} (${qty * fill.price:.2f}, x{size_mult:g})"
        return True

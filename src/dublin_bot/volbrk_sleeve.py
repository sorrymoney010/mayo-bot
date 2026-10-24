"""volbrk_1d PAPER sleeve: daily volatility breakout on BTC/ETH/SOL.

Signal logic matches /workspace/mayo-audit/v2_20261023/search.py ``sig(d,'volbrk',(k,h))``:

* ATR14 = simple rolling mean of true range (NOT Wilder/EWM);
* bar i fires when ``close[i] > close[i-1] + k*ATR[i-1]`` and ``close[i] > SMA100[i]``;
* the position is "on" for bars i..i+h-1; a fill happens at the next bar's open
  and the exit at the open h bars later (time stop). Signals on bars i..i+h
  (inclusive) are ignored, exactly like the research loop's ``left`` counter.

Paper accounting only: the gateway is built with ``allow_order_submission=False``
and the sleeve refuses to run unless every safety lock is engaged. Trades are
tagged ``volbrk_1d`` (learner / closed-trade key) so they score separately.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import numpy as np
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

SLEEVE = "volbrk_1d"
STRATEGY_KEY = "volbrk_1d"
VOLBRK_COINS = ("BTC/USD", "ETH/USD", "SOL/USD")
DAY = 86400


def sleeve_active(settings: Settings) -> tuple[bool, str]:
    if not getattr(settings, "volbrk_sleeve_enabled", False):
        return False, "disabled (VOLBRK_SLEEVE_ENABLED=false)"
    if not (settings.paper_trading and settings.dry_run and not settings.allow_live_trading):
        return False, "disabled: paper-only sleeve and a safety lock is off"
    return True, "active (paper only)"


# ── signal (research-exact) ────────────────────────────────────
def research_atr(d: pd.DataFrame, n: int = 14) -> pd.Series:
    tr = pd.concat([d.high - d.low, (d.high - d.close.shift()).abs(),
                    (d.low - d.close.shift()).abs()], axis=1).max(1)
    return tr.rolling(n).mean()


def volbrk_state_series(d: pd.DataFrame, k: float = 1.25, h: int = 10,
                        atr_n: int = 14, sma_n: int = 100) -> pd.Series:
    """Research ``sig(d,'volbrk',(k,h))`` — the in-position state per bar."""
    c = d.close
    a = research_atr(d, atr_n)
    e = c > c.rolling(sma_n).mean()
    st = np.zeros(len(c), bool)
    left = 0
    for i in range(1, len(c)):
        if left > 0:
            left -= 1
        elif c.iloc[i] > c.iloc[i - 1] + k * a.iloc[i - 1] and e.iloc[i]:
            left = h
        st[i] = left > 0
    return pd.Series(st, c.index)


def volbrk_signal(d: pd.DataFrame, k: float = 1.25, atr_n: int = 14, sma_n: int = 100) -> dict:
    """Signal on the LAST completed bar of ``d`` (no position-state memory)."""
    out = {"fire": False, "close": None, "prev_close": None, "atr_prev": None, "sma": None,
           "move": None, "threshold": None, "reason": ""}
    if d is None or len(d) < max(sma_n, atr_n + 2):
        out["reason"] = f"warmup: {0 if d is None else len(d)} bars < {max(sma_n, atr_n + 2)}"
        return out
    c = d.close.astype(float)
    a = research_atr(d.astype(float), atr_n)
    sma = c.rolling(sma_n).mean()
    close, prev, atr_prev, sma_v = float(c.iloc[-1]), float(c.iloc[-2]), float(a.iloc[-2]), float(sma.iloc[-1])
    move, thr = close - prev, k * atr_prev
    brk = bool(close > prev + thr)
    trend = bool(close > sma_v)
    out.update(close=close, prev_close=prev, atr_prev=atr_prev, sma=sma_v, move=move, threshold=thr,
               fire=brk and trend)
    parts = [f"move {move:+.6g} {'>' if brk else '<='} {k:g}*ATR {thr:.6g}",
             f"close {close:.6g} {'>' if trend else '<='} SMA{sma_n} {sma_v:.6g}"]
    out["reason"] = ("VOLBRK BUY: " if out["fire"] else "No volbrk entry: ") + "; ".join(parts)
    return out


def bars_held(d: pd.DataFrame, entry_bar_open: float) -> int:
    """Completed daily bars whose open >= the entry bar's open."""
    if d is None or not len(d):
        return 0
    ts = np.array([x.timestamp() for x in d.index])
    return int((ts >= float(entry_bar_open) - 1e-6).sum())


class VolBrkSleeve:
    def __init__(self, settings: Settings, *, gateway=None, audit: AuditLog | None = None,
                 now_fn: Callable[[], float] = time.time) -> None:
        self.base_settings = settings
        self.settings = settings.model_copy(update={
            "timeframe_minutes": 1440, "strategy": SLEEVE,
            "paper_taker_fee_bps": float(settings.volbrk_taker_fee_bps),
            "paper_slippage_bps": float(settings.volbrk_slippage_bps),
        })
        self.tf_seconds = DAY
        self.strategy_key = STRATEGY_KEY
        self.now_fn = now_fn
        self.audit = audit or AuditLog(Path(settings.audit_log_path))
        if gateway is None:
            from .engine import build_gateway
            gateway = build_gateway(self.settings, audit=self.audit, allow_order_submission=False)
        self.gateway = gateway
        self.risk = RiskManager(self.settings)
        self.fill_model = FillModel(self.settings)
        self.market_guard = MarketGuard(max_spread_bps=settings.max_spread_bps,
                                        min_dollar_volume=settings.min_dollar_volume)
        self.state_path = Path(settings.volbrk_state_path)
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

    @property
    def k(self) -> float:
        return float(self.settings.volbrk_k)

    @property
    def hold(self) -> int:
        return int(self.settings.volbrk_hold_bars)

    def universe(self) -> list[str]:
        out: list[str] = []
        for sym in self.settings.volbrk_symbols or []:
            sym = str(sym).strip().upper()
            if sym and sym not in out and sym in VOLBRK_COINS:
                out.append(sym)
        return out

    def _load_state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            data = {}
        for k, v in (("positions", {}), ("last_signal_bar", {}), ("blocked_through", {}),
                     ("last_eval", {}), ("events", [])):
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

    # ── time-stop exits ────────────────────────────────────────
    def _manage_positions(self, st, res, lots, state, now) -> None:
        for sym in list(st["positions"]):
            pos = st["positions"][sym]
            try:
                bars = self._bars(sym)
            except Exception as exc:
                res.errors.append(f"{sym} bars: {type(exc).__name__}: {exc}")
                res.symbols[sym] = "holding (bars unavailable)"
                continue
            held = bars_held(bars, float(pos["entry_bar_open"]))
            if held < self.hold:
                res.symbols[sym] = f"holding volbrk {held}/{self.hold} daily bars (time stop)"
                continue
            try:
                t = self.gateway.get_ticker_for(sym)
            except Exception as exc:
                res.errors.append(f"{sym} ticker: {type(exc).__name__}: {exc}")
                res.symbols[sym] = "time stop due but ticker unavailable (retry next cycle)"
                continue
            self._close_position(st, res, lots, state, sym, pos, t, f"time_stop_{self.hold}d", now)

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
            res.symbols[sym] = "already closed elsewhere"
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
                                      detail={"exit_reason": reason, "sleeve": SLEEVE})
        except Exception:
            pass
        from .state import record_close_pnl
        record_close_pnl(self.state_store.path, realized, self._equity(), self.settings, state)
        net_bps = realized / cost_basis * 1e4 if cost_basis > 0 else 0.0
        self._event(st, res, "exit", sym, reason=reason, price=fill.price, qty=sell_qty,
                    realized=round(realized, 6), net_bps=round(net_bps, 1), sleeve=SLEEVE)
        res.symbols[sym] = f"EXIT {reason} @ {fill.price:.6g} realized ${realized:+.2f} ({net_bps:+.0f} bps)"

    # ── entries ────────────────────────────────────────────────
    def _scan_entries(self, st, res, lots, state, now) -> None:
        self.registry.load()
        mine = set(st["positions"])
        held_all = {k for k, v in lots.items() if float(v) > 1e-9}
        blocked = self.registry.foreign_symbols(SLEEVE) | (held_all - mine)
        s = self.settings
        for sym in self.universe():
            if sym in mine:
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
            sig = volbrk_signal(bars, self.k, int(s.volbrk_atr_period), int(s.volbrk_sma_period))
            st["last_eval"][sym] = {"bar": bar_iso, "fire": sig["fire"], "reason": sig["reason"][:200]}
            if not sig["fire"]:
                res.symbols[sym] = f"[{bar_iso[:10]}] {sig['reason']}"[:200]
                continue
            if bar_open <= float(st["blocked_through"].get(sym, 0.0)):
                res.symbols[sym] = f"signal bar {bar_iso} inside previous hold window (ignored, as tested)"
                continue
            if st["last_signal_bar"].get(sym) == bar_iso:
                res.symbols[sym] = f"signal bar {bar_iso} already handled"
                continue
            if now - closed_at > self.tf_seconds:
                res.symbols[sym] = "signal bar older than one bar (not chased)"
                continue
            if sym in blocked:
                res.symbols[sym] = f"skip: {sym} held/pending by another sleeve ({self.registry.owner_of(sym)})"
                continue
            st["last_signal_bar"][sym] = bar_iso
            if self._try_buy(st, res, sym, sig, bar_open, lots, state, now):
                mine.add(sym)
                held_all.add(sym)

    def room(self, lots: dict[str, float], equity: float) -> tuple[float, float, int]:
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

    def size_notional(self, equity: float, price: float, atr_v: float) -> float:
        """Risk-style size: equity*risk / (mult*ATR/price), capped by max_position_fraction."""
        s = self.settings
        book = min(equity, float(s.strategy_equity_usd))
        dist = float(s.volbrk_sizing_atr_mult) * float(atr_v) / float(price) if price > 0 else 0.0
        if dist <= 0:
            return 0.0
        return min(book * float(s.volbrk_risk_per_trade) / dist, book * float(s.max_position_fraction))

    def _try_buy(self, st, res, sym, sig: dict, bar_open, lots, state, now) -> bool:
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
        atr_v = float(sig["atr_prev"])
        stop_ref = price - float(s.volbrk_sizing_atr_mult) * atr_v
        probe = Signal(Action.BUY, 100, sig["reason"][:120], price, atr_v, max(stop_ref, price * 0.5))
        decision = self.risk.evaluate(probe, state, open_exposure_usd=exposure)
        if not decision.approved:
            res.symbols[sym] = f"BUY blocked: risk: {decision.reason}"
            return False
        cash = self.portfolio.snapshot().cash - self.registry.pending_notional()
        notional = min(self.size_notional(equity, price, atr_v) * size_mult, cap - exposure, cash * 0.995)
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
        entry_bar_open = float(bar_open) + DAY
        st["positions"][sym] = {"entry": fill.price, "qty": qty, "entry_fee": fill.fee, "filled_at": now,
                                "signal_bar": _iso(bar_open), "entry_bar_open": entry_bar_open,
                                "signal_px": float(sig["close"]), "atr_prev": atr_v, "k": self.k,
                                "hold_bars": self.hold, "size_mult": size_mult, "sleeve": SLEEVE}
        # research: signals on bars i..i+h are ignored (the hold window + exit bar)
        st["blocked_through"][sym] = float(bar_open) + self.hold * DAY
        try:
            self.learner.note_entry(sym, regime="trend", notional=qty * fill.price + fill.fee)
        except Exception:
            pass
        state.orders_today += 1
        state.last_order_at = datetime.fromtimestamp(now, tz=timezone.utc)
        self._event(st, res, "entry", sym, price=fill.price, qty=qty, fee=round(fill.fee, 6),
                    notional=round(qty * fill.price, 2), size_mult=size_mult, signal_bar=_iso(bar_open),
                    sleeve=SLEEVE)
        res.symbols[sym] = f"BOUGHT paper {qty:g} @ {fill.price:.6g} (${qty * fill.price:.2f}, x{size_mult:g})"
        return True

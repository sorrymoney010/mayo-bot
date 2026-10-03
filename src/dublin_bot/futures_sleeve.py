"""Paper-only Kraken perpetual SHORT sleeve.

Public data only: spot OHLC for the daily/EMA filter, and the Futures public
ticker for mark, funding and open interest. Fills are simulated. Leverage is
hard-capped at 2x. The sleeve is OFF unless ``FUTURES_SLEEVE_ENABLED`` is set
and every safety lock is still engaged (paper, dry-run, no live).

It never imports ``venue_locks.submit_futures_order``.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from .book_risk import futures_equity_addon
from .config import Settings
from .futures_costs import (
    MAX_PAPER_LEVERAGE,
    SPOT_TO_PERP,
    clamp_leverage,
    fee_usd,
    funding_cashflow,
    liquidation_price,
    short_pnl,
)
from .futures_public import FuturesPublic
from .paper import PaperPortfolio
from .sleeve_registry import SleeveRegistry
from .technicals import atr, ema

SLEEVE = "futures_short"
LEDGER_PATH = Path("logs/paper_futures.json")
PAPER_BOOK = Path("logs/paper_portfolio.json")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def sleeve_active(settings: Settings) -> tuple[bool, str]:
    if not getattr(settings, "futures_sleeve_enabled", False):
        return False, "disabled (FUTURES_SLEEVE_ENABLED=false)"
    if not (settings.paper_trading and settings.dry_run and not settings.allow_live_trading):
        return False, "disabled: paper-only sleeve and a safety lock is off"
    tf = int(getattr(settings, "futures_timeframe_minutes", 240) or 240)
    if tf < 60:
        return False, f"disabled: futures_timeframe_minutes={tf} is below 1h (entries refused)"
    return True, "active (paper only, public futures data)"


def load_ledger(path: Path | str = LEDGER_PATH) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("positions", {})
            return data
    except (OSError, ValueError):
        pass
    return {"positions": {}}


def save_ledger(data: dict, path: Path | str = LEDGER_PATH) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)


@dataclass
class FuturesCycle:
    sleeve: str = SLEEVE
    active: bool = False
    reason: str = ""
    actions: list[dict] = field(default_factory=list)
    symbols: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"sleeve": self.sleeve, "active": self.active, "reason": self.reason,
                "actions": self.actions, "symbols": self.symbols, "errors": self.errors}


class FuturesSleeve:
    def __init__(self, settings: Settings, *, gateway=None, public: FuturesPublic | None = None,
                 portfolio: PaperPortfolio | None = None, ledger_path: Path | str = LEDGER_PATH,
                 now_fn: Callable[[], float] = time.time, bars_fn: Callable[[str], pd.DataFrame] | None = None,
                 daily_fn: Callable[[str], dict] | None = None) -> None:
        self.settings = settings
        self.now_fn = now_fn
        self.public = public or FuturesPublic()
        self.portfolio = portfolio or PaperPortfolio(PAPER_BOOK)
        self.ledger_path = Path(ledger_path)
        self.registry = SleeveRegistry()
        self._gateway = gateway
        self._bars_fn = bars_fn
        self._daily_fn = daily_fn
        self.tf = int(getattr(settings, "futures_timeframe_minutes", 240) or 240)
        self.leverage = clamp_leverage(getattr(settings, "futures_leverage", 1.0))
        self.maintenance = float(getattr(settings, "futures_maintenance_margin", 0.01) or 0.01)

    def universe(self) -> list[str]:
        raw = getattr(self.settings, "futures_symbols", None) or ["BTC/USD", "ETH/USD", "SOL/USD"]
        out = []
        for sym in raw:
            sym = str(sym).strip().upper()
            if sym in SPOT_TO_PERP and sym not in out:
                out.append(sym)
        return out

    def _bars(self, symbol: str) -> pd.DataFrame:
        if self._bars_fn is not None:
            return self._bars_fn(symbol)
        from .engine import build_gateway
        gw = self._gateway
        if gw is None:
            gw = build_gateway(self.settings, allow_order_submission=False)
            self._gateway = gw
        saved = self.settings.symbol
        try:
            self.settings.symbol = symbol
            self.settings.timeframe_minutes = self.tf
            return gw.get_bars()
        finally:
            self.settings.symbol = saved

    def _daily(self, symbol: str) -> dict:
        if self._daily_fn is not None:
            return self._daily_fn(symbol)
        from .daily_filter import DailyFilter
        return DailyFilter().state(symbol)

    def _equity(self) -> float:
        seed = float(self.settings.strategy_equity_usd)
        snap = self.portfolio.load(equity=seed, cash=seed)
        basis = sum(p.quantity * p.entry_price for p in snap.positions.values())
        return max(snap.cash + basis, 0.0) + futures_equity_addon(self.ledger_path)

    def run_cycle(self) -> FuturesCycle:
        res = FuturesCycle()
        ok, why = sleeve_active(self.settings)
        res.active, res.reason = ok, why
        if not ok:
            return res
        if self.leverage > MAX_PAPER_LEVERAGE + 1e-9:
            res.errors.append("leverage above 2x refused")
            res.active = False
            return res
        now = float(self.now_fn())
        book = load_ledger(self.ledger_path)
        self._mark_and_risk(book, res, now)
        save_ledger(book, self.ledger_path)
        self._entries(book, res, now)
        self._publish(book)
        save_ledger(book, self.ledger_path)
        return res

    def _quote(self, symbol: str) -> dict | None:
        perp = SPOT_TO_PERP[symbol]
        try:
            snap = self.public.snapshot(perp)
        except Exception:  # public read failed — fail closed for this symbol
            return None
        if not snap.get("mark"):
            return None
        snap["oi_note"] = snap.get("open_interest")
        return snap

    def _mark_and_risk(self, book: dict, res: FuturesCycle, now: float) -> None:
        for sym in list(book["positions"]):
            pos = book["positions"][sym]
            quote = self._quote(sym)
            if quote is None:
                res.errors.append(f"{sym} public futures quote unavailable")
                res.symbols[sym] = "holding (public quote unavailable)"
                continue
            mark = float(quote["mark"])
            notional = abs(float(pos["notional"]))
            pos["unrealized"] = short_pnl(float(pos["entry"]), mark, notional)
            pos["last_mark"] = mark
            pos["open_interest"] = quote.get("open_interest")
            rate = quote.get("funding_rate_relative")
            if rate is None:
                rate = quote.get("funding_rate")
                mark_px = quote.get("mark")
                if rate is not None and mark_px:
                    from .futures_costs import relative_funding_rate
                    rate = relative_funding_rate(rate, mark_px)
            if rate is not None and pos.get("last_funding_ts"):
                hours = max(0.0, (now - float(pos["last_funding_ts"])) / 3600.0)
                # Kraken realises funding every hour. Accrue whole hours only.
                whole = int(hours)
                if whole >= 1:
                    cash = funding_cashflow(side="short", notional=notional,
                                            hourly_rate=float(rate), hours=whole)
                    self.portfolio.adjust_cash(cash)
                    pos["funding_usd"] = float(pos.get("funding_usd") or 0.0) + cash
                    pos["last_funding_ts"] = float(pos["last_funding_ts"]) + whole * 3600.0
                    res.actions.append({"event": "funding", "symbol": sym, "usd": round(cash, 6),
                                        "hours": whole, "rate": rate})
            elif rate is not None and not pos.get("last_funding_ts"):
                pos["last_funding_ts"] = now
            reason = None
            exit_px = mark
            if mark >= float(pos["liq"]):
                reason, exit_px = "liquidation", float(pos["liq"])
            else:
                reason, exit_px = self._signal_exit(sym, pos, mark, now)
            if reason:
                self._close(book, sym, pos, exit_px, reason, res, now)
            else:
                res.symbols[sym] = (f"SHORT holding mark {mark:.6g} liq {float(pos['liq']):.6g} "
                                    f"oi={quote.get('open_interest')}")

    def _signal_exit(self, sym: str, pos: dict, mark: float, now: float):
        try:
            bars = self._bars(sym)
        except Exception:
            return None, mark
        if bars is None or len(bars) < 5:
            return None, mark
        closed_at = _bar_close_ts(bars, self.tf)
        if closed_at <= float(pos.get("filled_at") or 0):
            return None, mark
        slow = int(getattr(self.settings, "futures_ema_slow", 100) or 100)
        es = ema(bars["close"], slow)
        if float(bars["close"].iloc[-1]) > float(es.iloc[-1]):
            return "above_ema", mark
        if sleeve_trail_hit(self.settings, sym, pos, mark, bars):
            return "trail", mark
        return None, mark

    def _close(self, book, sym, pos, exit_px, reason, res, now) -> None:
        from .paper import paper_book_lock
        with paper_book_lock(self.portfolio.path):
            disk = load_ledger(self.ledger_path)
            if sym not in (disk.get("positions") or {}):
                book["positions"].pop(sym, None)
                res.symbols[sym] = "already closed"
                return
            notional = abs(float(pos["notional"]))
            pnl = short_pnl(float(pos["entry"]), float(exit_px), notional)
            exit_fee = fee_usd(notional)
            margin = float(pos["margin"])
            # Entry fee and margin already left cash. Return margin and price P&L, pay exit fee.
            self.portfolio.adjust_cash(margin + pnl - exit_fee)
            book["positions"].pop(sym, None)
            save_ledger(book, self.ledger_path)
        res.actions.append({
            "event": "exit", "symbol": sym, "reason": reason, "price": exit_px,
            "pnl": round(pnl - exit_fee - float(pos.get("fees_usd") or 0.0), 6),
            "funding_usd": round(float(pos.get("funding_usd") or 0.0), 6),
        })
        res.symbols[sym] = f"EXIT {reason} @ {exit_px:.6g}"
        book["positions"].pop(sym, None)
        self._drop_trail(sym)

    def _entries(self, book: dict, res: FuturesCycle, now: float) -> None:
        max_n = int(getattr(self.settings, "max_concurrent_positions", 3) or 3)
        for sym in self.universe():
            if sym in book["positions"]:
                continue
            self.registry.load()
            if sym in self.registry.foreign_symbols(SLEEVE) or self._spot_held(sym):
                res.symbols[sym] = f"skip: {sym} already held on the spot book"
                continue
            if len(book["positions"]) + self._spot_count() >= max_n:
                res.symbols[sym] = f"SHORT blocked: max concurrent positions ({max_n})"
                continue
            daily = self._daily(sym)
            if daily.get("riskon") is not False:
                res.symbols[sym] = f"no short: D1 {daily.get('reason')}"
                continue
            try:
                bars = self._bars(sym)
            except Exception as exc:
                res.errors.append(f"{sym} bars: {type(exc).__name__}")
                continue
            fast = int(getattr(self.settings, "futures_ema_fast", 20) or 20)
            slow = int(getattr(self.settings, "futures_ema_slow", 100) or 100)
            if bars is None or len(bars) < slow + 2:
                res.symbols[sym] = "no short: bars short"
                continue
            ef, es = ema(bars["close"], fast), ema(bars["close"], slow)
            if not (float(bars["close"].iloc[-1]) < float(es.iloc[-1]) and float(ef.iloc[-1]) < float(es.iloc[-1])):
                res.symbols[sym] = "no short: trend not bearish"
                continue
            quote = self._quote(sym)
            if quote is None:
                res.symbols[sym] = "no short: public perp quote unavailable"
                continue
            if not self._try_open(book, sym, quote, res, now):
                continue
            save_ledger(book, self.ledger_path)

    def _try_open(self, book, sym, quote, res, now) -> bool:
        mark = float(quote["mark"])
        equity = self._equity()
        cap = equity * float(self.settings.max_exposure_fraction)
        exposure = sum(abs(float(p.get("notional") or 0.0)) for p in book["positions"].values())
        exposure += self._spot_exposure_guess()
        room = cap - exposure
        if room <= 0:
            res.symbols[sym] = "SHORT blocked: exposure cap"
            return False
        snap = self.portfolio.snapshot()
        cash = float(snap.cash)
        lev = self.leverage
        margin_budget = min(equity * float(getattr(self.settings, "futures_margin_fraction", 0.25) or 0.25),
                            room / lev, cash * 0.95)
        notional = margin_budget * lev
        if notional < float(self.settings.min_order_notional_usd):
            res.symbols[sym] = "SHORT blocked: size below minimum"
            return False
        # Notional itself must sit inside the remaining exposure room.
        if notional > room:
            notional = room
            margin_budget = notional / lev
        open_fee = fee_usd(notional)
        if cash < margin_budget + open_fee:
            res.symbols[sym] = "SHORT blocked: insufficient paper cash"
            return False
        self.portfolio.adjust_cash(-(margin_budget + open_fee))
        liq = liquidation_price(mark, side="short", leverage=lev, maintenance=self.maintenance)
        book["positions"][sym] = {
            "perp": SPOT_TO_PERP[sym], "side": "short", "entry": mark,
            "notional": notional, "margin": margin_budget, "leverage": lev,
            "liq": liq, "filled_at": now, "opened_at": _iso(now),
            "last_funding_ts": now, "funding_usd": 0.0, "fees_usd": open_fee,
            "unrealized": 0.0, "last_mark": mark, "open_interest": quote.get("open_interest"),
        }
        res.actions.append({"event": "entry", "symbol": sym, "perp": SPOT_TO_PERP[sym],
                            "price": mark, "notional": round(notional, 2),
                            "leverage": lev, "liq": liq, "oi": quote.get("open_interest")})
        res.symbols[sym] = (f"SHORT paper {SPOT_TO_PERP[sym]} notional ${notional:.2f} "
                            f"@ {mark:.6g} lev {lev:g}x liq {liq:.6g}")
        return True

    def _publish(self, book: dict) -> None:
        owned = set(book["positions"])
        self.registry.load()
        self.registry.set_sleeve(SLEEVE, owned=owned, pending={})
        self.registry.save()

    def _spot_held(self, symbol: str) -> bool:
        try:
            data = json.loads(Path("logs/paper_bot_positions.json").read_text(encoding="utf-8"))
            return float(data.get(symbol, 0.0) or 0.0) > 1e-9
        except (OSError, ValueError, TypeError):
            return False

    def _spot_count(self) -> int:
        try:
            data = json.loads(Path("logs/paper_bot_positions.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return 0
        return sum(1 for v in data.values() if float(v or 0) > 1e-9)

    def _spot_exposure_guess(self) -> float:
        """Cost basis of spot lots (no extra ticker call). Fail-open only if the file is absent."""
        try:
            data = json.loads(PAPER_BOOK.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0.0
        total = 0.0
        for p in data.get("positions") or []:
            try:
                total += float(p.get("quantity") or 0) * float(p.get("entry_price") or 0)
            except (TypeError, ValueError):
                return float("inf")
        return total

    def _drop_trail(self, symbol: str) -> None:
        try:
            from .trailing import TrailBook
            book = TrailBook(getattr(self.settings, "trailing_state_path", "logs/trailing_state.json"))
            book.states = {k: v for k, v in book.states.items()
                           if not (v.symbol == symbol and v.sleeve == SLEEVE)}
            book.save()
        except Exception:
            pass


def sleeve_trail_hit(settings, symbol: str, pos: dict, mark: float, bars) -> bool:
    from .trailing import TrailBook, fresh_state, on_price, sleeve_trail_enabled
    if not sleeve_trail_enabled(settings, SLEEVE):
        return False
    try:
        series = atr(bars, 14)
        atr_v = float(series.iloc[-1])
    except Exception:
        return False
    if not (atr_v > 0):
        return False
    path = getattr(settings, "trailing_state_path", "logs/trailing_state.json")
    store = TrailBook(path)
    opened = str(pos.get("opened_at") or "")
    seed = fresh_state(
        sleeve=SLEEVE, symbol=symbol, side="short", entry=float(pos["entry"]), atr=atr_v,
        activate_atr=float(settings.trailing_activate_atr), trail_atr=float(settings.trailing_atr_mult),
        opened_at=opened, last_price=mark,
    )
    st = store.get(seed)
    st, reason = on_price(st, mark)
    if reason:
        store.drop(st)
        store.save()
        return True
    store.put(st)
    store.save()
    return False


def _bar_close_ts(bars: pd.DataFrame, tf_minutes: int) -> float:
    idx = bars.index[-1]
    try:
        return float(idx.timestamp()) + tf_minutes * 60
    except Exception:
        return 0.0

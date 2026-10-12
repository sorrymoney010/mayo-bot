"""Alt-coin regime_trend@240m PAPER sleeve (simulated fills only).

Rules = ``backtest_core.regime_signals`` (ADX 25/20 hysteresis, close > EMA200,
EMA20 > EMA50, close > prior 20-bar high, D1 filter OFF) on CLOSED public 4h
Kraken bars for 8 alt coins. Exit: 3% hard stop (fast exit watcher + cycle
backup), chandelier (22-bar HH - 3xATR) or ADX chop on a closed 4h bar, and the
regime trailing stop when ``trailing_tp_regime`` is on (same as regime_trend).

ISOLATION (deliberate): the sleeve keeps its own paper sub-book
``logs/alt/paper_portfolio.json`` (seeded at STRATEGY_EQUITY_USD, never mixed
with ``logs/paper_portfolio.json``). It never writes ``paper_bot_positions.json``
or the sleeve registry, so the BTC/ETH/SOL slot pool and cash are untouched. Its
P&L DOES reach the shared breakers: closes call ``record_close_pnl`` and every
equity reader adds ``book_risk.alt_equity_addon`` (combined equity). Its own caps: 1 position per coin, at most
``alt_regime_max_open_positions`` (3), ``alt_regime_position_fraction`` (5%) of
the book each. Fills: ``FillModel`` (paper taker fee + slippage) at the PUBLIC
ticker. No gateway, no keys, no orders. Refuses to run unless paper + dry-run +
no-live.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .backtest_core import add_indicators, regime_signals
from .alt_regime_shadow import PAIR, _public_ohlc
from .exit_watcher import ExitWatcher, claim_key, claim_opened, _read_json
from .fills import FillModel
from .paper import PaperPortfolio

SLEEVE = "alt_regime_4h"
STRATEGY_KEY = "alt_regime@240m"
ALT_DIR = Path("logs/alt")
ALT_BOOK = ALT_DIR / "paper_portfolio.json"
ALT_STATE = ALT_DIR / "alt_regime_sleeve.json"
ALT_EVENTS = ALT_DIR / "alt_regime_sleeve.jsonl"
SETTLE = 90


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


def public_ticker(symbol: str) -> dict:
    from .pipeline.kraken_rest import KrakenPublic
    pair = PAIR.get(symbol, symbol.replace("/", ""))
    res = KrakenPublic(min_interval=0.3, timeout=6.0).get("Ticker", {"pair": pair}, retries=1)
    raw = next(iter(res.values()))
    return {"bid": float(raw["b"][0]), "ask": float(raw["a"][0]), "last": float(raw["c"][0])}


def sleeve_active(settings) -> tuple[bool, str]:
    if not getattr(settings, "alt_regime_enabled", False):
        return False, "disabled (ALT_REGIME_ENABLED=false)"
    if not (settings.paper_trading and settings.dry_run and not settings.allow_live_trading):
        return False, "disabled: paper-only sleeve and a safety lock is off"
    return True, "active (paper only, own sub-book)"


def alt_exit_levels(state_path: Path, symbol: str) -> tuple[str, float | None, float | None, str]:
    pos = (_read_json(Path(state_path), {}).get("positions") or {}).get(symbol)
    if isinstance(pos, dict) and float(pos.get("qty") or 0) > 0:
        return SLEEVE, float(pos["stop"]), None, str(pos.get("filled_at") or "")
    return SLEEVE, None, None, ""


class AltRegimeSleeve:
    def __init__(self, settings, *, base_dir: Path = ALT_DIR,
                 fetch: Callable | None = None, ticker: Callable | None = None,
                 now_fn: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self.base = Path(base_dir)
        self.book_path = self.base / "paper_portfolio.json"
        self.state_path = self.base / "alt_regime_sleeve.json"
        self.events_path = self.base / "alt_regime_sleeve.jsonl"
        self.portfolio = PaperPortfolio(self.book_path)
        self.fill_model = FillModel(settings)
        self.fetch = fetch or _public_ohlc
        self.ticker = ticker or public_ticker
        self.now_fn = now_fn
        self.tf = int(getattr(settings, "alt_regime_timeframe_minutes", 240))
        self.symbols = [str(s).upper() for s in getattr(settings, "alt_regime_symbols", [])]
        self.max_open = int(getattr(settings, "alt_regime_max_open_positions", 3))
        self.frac = float(getattr(settings, "alt_regime_position_fraction", 0.05))
        self.seed = float(settings.strategy_equity_usd)
        self.p = dict(adx_enter=float(settings.adx_enter_above), adx_exit=float(settings.adx_exit_below),
                      lookback=20, atr_mult=3.0, stop=0.03, d1=False)
        self.base.mkdir(parents=True, exist_ok=True)
        seed_file = self.base / "seed.json"
        if not seed_file.exists():
            seed_file.write_text(json.dumps({"seed": self.seed}), encoding="utf-8")

    # ── files ──
    def load_state(self) -> dict:
        st = _read_json(self.state_path, {})
        st = st if isinstance(st, dict) else {}
        for k, v in (("positions", {}), ("last_bar", 0), ("last_signal_bar", {})):
            st.setdefault(k, v)
        return st

    def save_state(self, st: dict) -> None:
        st["updated_at"] = _iso(self.now_fn())
        st["strategy_key"] = STRATEGY_KEY
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2, default=str), encoding="utf-8")
        tmp.replace(self.state_path)

    def _event(self, kind: str, **kw) -> dict:
        ev = {"ts": _iso(self.now_fn()), "sleeve": SLEEVE, "paper": True, "event": kind, **kw}
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, default=str) + "\n")
        return ev

    def _bar_target(self, now: float) -> int:
        tf = self.tf * 60
        return int((now - SETTLE) // tf * tf) - tf

    # ── cycle ──
    def run_cycle(self) -> dict:
        ok, why = sleeve_active(self.settings)
        out = {"sleeve": SLEEVE, "active": ok, "reason": why, "actions": [], "errors": []}
        if not ok:
            return out
        now = float(self.now_fn())
        snap = self.portfolio.load(equity=self.seed, cash=self.seed)
        st = self.load_state()
        for sym in list(st["positions"]):  # closed by the fast watcher
            if sym not in snap.positions:
                st["positions"].pop(sym)
                out["actions"].append(self._event("position_gone", symbol=sym, note="closed by exit watcher"))
        # every cycle: stop backup + trail upkeep from the public ticker
        for sym in list(st["positions"]):
            pos = st["positions"][sym]
            try:
                t = self.ticker(sym)
            except Exception as exc:  # noqa: BLE001
                out["errors"].append(f"{sym} ticker: {type(exc).__name__}")
                continue
            if t["last"] <= float(pos["stop"]):
                self._close(st, out, sym, t, "stop", now)
                continue
            from .trailing import maintain
            try:
                if maintain(self.settings, SLEEVE, sym, "long", float(pos["entry"]), t["last"],
                            str(pos["filled_at"]), float(pos.get("atr") or 0)) == "trail":
                    self._close(st, out, sym, t, "trail", now)
            except Exception as exc:  # noqa: BLE001
                out["errors"].append(f"{sym} trail: {type(exc).__name__}")
        target = self._bar_target(now)
        if target > int(st.get("last_bar") or 0):
            self._bar_cycle(st, out, target, now)
            st["last_bar"] = target
        self.save_state(st)
        out["open"] = sorted(st["positions"])
        return out

    def _bar_cycle(self, st, out, target, now) -> None:
        for sym in self.symbols:
            try:
                df = self.fetch(sym, self.tf)
                df = df[df["time"] <= target]
                if len(df) < 260 or int(df["time"].iloc[-1]) != target:
                    out["errors"].append(f"{sym}: closed bar {target} not available")
                    continue
                d = add_indicators(df)
                sig = regime_signals(d, self.p)
                i = len(d) - 1
                if sym in st["positions"]:
                    pos = st["positions"][sym]
                    if float(pos["filled_at_ts"]) >= target + self.tf * 60:
                        continue  # no bar closed since the fill
                    why = "chandelier" if sig["chandelier_exit"][i] else ("regime_chop" if not sig["adx_on"][i] else "")
                    if why:
                        self._close(st, out, sym, self.ticker(sym), why, now)
                    continue
                if not bool(sig["entry"][i]):
                    continue
                if st["last_signal_bar"].get(sym) == target:
                    continue
                st["last_signal_bar"][sym] = target
                self._buy(st, out, sym, float(d["atr"].iloc[i]), target, now)
            except Exception as exc:  # noqa: BLE001
                out["errors"].append(f"{sym}: {type(exc).__name__}: {exc}"[:200])

    def _buy(self, st, out, sym, atr, target, now) -> None:
        if len(st["positions"]) >= self.max_open:
            out["actions"].append(self._event("skip", symbol=sym, reason=f"max_open={self.max_open}"))
            return
        t = self.ticker(sym)
        notional = self.seed * self.frac
        snap = self.portfolio.load(equity=self.seed, cash=self.seed)
        price = float(t["ask"])
        if not (price > 0 and math.isfinite(price)):
            return
        qty = round(notional / price, 8)
        fill = self.fill_model.buy(price=float(t["last"]), volume=qty, bid=t.get("bid"), ask=t.get("ask"))
        if snap.cash < qty * fill.price + fill.fee:
            out["actions"].append(self._event("skip", symbol=sym, reason="insufficient sub-book cash"))
            return
        self.portfolio.record_buy(symbol=sym, quantity=qty, fill_price=fill.price, fee=fill.fee, when=_iso(now))
        st["positions"][sym] = {"entry": fill.price, "qty": qty, "fee": fill.fee, "stop": fill.price * (1 - self.p["stop"]),
                                "atr": atr, "filled_at": _iso(now), "filled_at_ts": now, "signal_bar": target}
        out["actions"].append(self._event("entry", symbol=sym, price=fill.price, qty=qty,
                                          notional=round(qty * fill.price, 2), fee=round(fill.fee, 6),
                                          stop=round(fill.price * (1 - self.p["stop"]), 8)))

    def _close(self, st, out, sym, t, reason, now) -> None:
        pos = st["positions"][sym]
        fill = self.fill_model.sell(price=float(t["last"]), volume=float(pos["qty"]), bid=t.get("bid"), ask=t.get("ask"))
        realized = self.portfolio.try_record_sell(sym, float(pos["qty"]), fill.price, fill.fee, _iso(now))
        st["positions"].pop(sym, None)
        if realized is None:
            return
        basis = float(pos["qty"]) * float(pos["entry"]) + float(pos.get("fee") or 0)
        out["actions"].append(self._event("exit", symbol=sym, reason=reason, price=fill.price,
                                          realized=round(realized, 6),
                                          net_bps=round(realized / basis * 1e4, 1) if basis else 0.0))
        _learn(self.settings, sym, realized, basis, reason)
        account_close(self.settings, realized)


def combined_equity(settings) -> float:
    """Main paper book (cash + basis) + futures addon + alt addon (see book_risk)."""
    from .book_risk import futures_equity_addon
    seed = float(settings.strategy_equity_usd)
    snap = PaperPortfolio(Path("logs/paper_portfolio.json")).load(equity=seed, cash=seed)
    basis = sum(p.quantity * p.entry_price for p in snap.positions.values())
    return max(snap.cash + basis, 0.0) + futures_equity_addon(getattr(settings, "futures_ledger_path", None))


def account_close(settings, realized: float) -> None:
    """Alt closes land in the SHARED session (daily-loss halt, streak, drawdown)."""
    try:
        from .state import record_close_pnl
        record_close_pnl(settings.session_state_path, float(realized), combined_equity(settings), settings)
    except Exception:
        pass


def _learn(settings, sym, realized, notional, reason, fast=False) -> None:
    try:
        from .learner import enqueue_learner_exit
        enqueue_learner_exit(settings.learner_path, symbol=sym, pnl=float(realized), strategy=STRATEGY_KEY,
                             notional=abs(float(notional)), ts=time.time(),
                             detail={"exit_reason": reason, "fast_exit": fast, "sleeve": STRATEGY_KEY})
    except Exception:
        pass


class AltExitWatcher(ExitWatcher):
    """Fast stop/trail for the alt sub-book. Own claims/lots files under logs/alt/;
    public REST ticker poll (only while a lot is open); no session/registry writes."""

    def __init__(self, settings, *, base_dir: Path = ALT_DIR, held_lock=None, ticker=None) -> None:
        super().__init__(settings, portfolio=PaperPortfolio(Path(base_dir) / "paper_portfolio.json"),
                         held_lock=held_lock)
        self.state_path = Path(base_dir) / "alt_regime_sleeve.json"
        self.error_log = Path(base_dir) / "alt_exit_watcher.log"
        self.ticker = ticker or public_ticker

    def _spot(self, symbol, price, ts):
        snap = self.portfolio.load(equity=float(self.settings.strategy_equity_usd),
                                   cash=float(self.settings.strategy_equity_usd))
        pos = snap.positions.get(symbol)
        if pos is None or pos.quantity <= 1e-12:
            return None
        sleeve, stop, _tp, opened = alt_exit_levels(self.state_path, symbol)
        if stop is None:
            return None
        opened = claim_opened(sleeve, opened, pos)
        state = (_read_json(self.state_path, {}).get("positions") or {}).get(symbol) or {}
        reason = "stop" if price <= stop else self._trail(sleeve, symbol, "long",
                                                          float(state.get("entry") or pos.entry_price), price, opened)
        if not reason:
            return None
        return self._book_spot(symbol, sleeve, pos.quantity, price, reason, False, opened, ts)

    def _open_claim_keys(self):
        equity = float(self.settings.strategy_equity_usd)
        snap = self.portfolio.load(equity=equity, cash=equity)
        keys = set()
        for sym, pos in snap.positions.items():
            sleeve, _s, _t, opened = alt_exit_levels(self.state_path, sym)
            keys.add(claim_key(sleeve, sym, claim_opened(sleeve, opened, pos)))
        return keys

    def _release_owner(self, symbol):  # alt coins are not in the shared registry
        return None

    def _account(self, realized):  # shared breakers see alt P&L (combined equity)
        account_close(self.settings, realized)

    def _learn(self, symbol, sleeve, realized, qty, price, reason):
        _learn(self.settings, symbol, realized, qty * price, reason, fast=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                snap = self.portfolio.load(equity=self.seed_equity, cash=self.seed_equity)
                for sym in list(snap.positions):
                    try:
                        px = float(self.ticker(sym)["last"])
                        if px > 0:
                            self.evaluate(sym, px, time.time())
                    except Exception as exc:  # noqa: BLE001
                        self._note_error(exc)
            except Exception as exc:  # noqa: BLE001
                self._note_error(exc)
            self._stop.wait(5.0)

    @property
    def seed_equity(self) -> float:
        return float(self.settings.strategy_equity_usd)

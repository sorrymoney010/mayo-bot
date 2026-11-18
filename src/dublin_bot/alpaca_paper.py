"""Alpaca paper runner: the three fee-surviving sleeves on Alpaca's paper account.

Sleeves (defaults = the walk-forward's default specs, which the folds chose
almost every time; see data/walkforward_results.json):

* ``regime_1h``     regime trend, 1h, market entry
* ``regime_4h_mk``  regime trend, 4h, limit entry 0.1% under the signal close
* ``meanrev_4h_mk`` mean reversion, 4h, limit entry 0.1% under the signal close

Rules match ``backtest_core.simulate`` so paper results are comparable:

* signals on CLOSED bars only (shared ``regime_signals`` / ``meanrev_signals``)
* market entry right after the signal bar closes (≈ next bar's open)
* limit entry rests for ONE bar; unfilled → cancelled, no trade
* hard stop 3% under entry, take-profit 25% over entry (polled every cycle)
* signal exits: regime → chandelier break or ADX chop; meanrev → reverted
* long-only spot, one position per sleeve per symbol

Every slot is (sleeve, symbol). Alpaca nets positions per symbol, so the runner
keeps its own lots in a JSON state file and only ever sells its own quantity.
Orders carry deterministic client ids, so a restart never doubles an order.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .alpaca_gateway import OPEN_STATUSES, AlpacaGateway
from .backtest_core import add_indicators, meanrev_signals, regime_signals

DEFAULT_SYMBOLS = ("BTC/USD", "ETH/USD", "SOL/USD")
WARM_BARS = 210          # same warm-up as the backtester (EMA200 needs it)
FETCH_BARS = 400
ENTRY_FRESH_SECONDS = 15 * 60


@dataclass(frozen=True)
class SleeveSpec:
    name: str
    family: str             # "regime" | "meanrev"
    tf: int                 # minutes
    entry: str              # "market" | "limit"
    params: dict

    @property
    def stop(self) -> float:
        return float(self.params.get("stop", 0.03))

    @property
    def tp(self) -> float:
        return float(self.params.get("tp", 0.25))


_REGIME = {"adx_enter": 25.0, "adx_exit": 20.0, "atr_mult": 3.0, "lookback": 20,
           "min_atr_rank": 0.0, "stop": 0.03, "tp": 0.25}
_MEANREV = {"rsi_os": 38.0, "rsi_exit": 55.0, "stop": 0.03, "tp": 0.25}

SLEEVES: dict[str, SleeveSpec] = {
    "regime_1h": SleeveSpec("regime_1h", "regime", 60, "market", dict(_REGIME)),
    "regime_4h_mk": SleeveSpec("regime_4h_mk", "regime", 240, "limit",
                               {**_REGIME, "entry": "limit", "limit_offset": 0.001}),
    "meanrev_4h_mk": SleeveSpec("meanrev_4h_mk", "meanrev", 240, "limit",
                                {**_MEANREV, "entry": "limit", "limit_offset": 0.001}),
}


@dataclass
class Slot:
    sleeve: str
    symbol: str
    status: str = "flat"            # flat | entering | open | exiting
    last_bar: float = 0.0           # start time (epoch s) of the last processed closed bar
    entry_order_id: str = ""
    entry_client_id: str = ""
    entry_expires: float = 0.0      # epoch s; limit entries are cancelled after this
    entry_maker: bool = False
    signal_bar: float = 0.0
    qty: float = 0.0
    entry_px: float = 0.0
    entry_time: str = ""
    stop_px: float = 0.0
    tp_px: float = 0.0
    exit_order_id: str = ""
    exit_client_id: str = ""
    exit_reason: str = ""
    exit_attempts: int = 0

    @property
    def key(self) -> str:
        return f"{self.sleeve}|{self.symbol}"

    def reset(self) -> None:
        keep = {"sleeve": self.sleeve, "symbol": self.symbol, "last_bar": self.last_bar}
        self.__dict__.update(asdict(Slot(**keep)))


@dataclass
class RunnerConfig:
    state_path: Path = Path("data/alpaca_paper/state.json")
    journal_path: Path = Path("data/alpaca_paper/trades.jsonl")
    events_path: Path = Path("data/alpaca_paper/events.jsonl")
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS
    sleeves: tuple[str, ...] = tuple(SLEEVES)
    budget_usd: float = 100.0
    risk_per_trade: float = 0.01
    max_slot_fraction: float | None = None   # default: 1 / number of slots
    max_entries_per_day: int = 6
    max_daily_loss_fraction: float = 0.03
    taker_bps: float = 25.0
    maker_bps: float = 15.0


@dataclass
class RunnerState:
    slots: dict[str, Slot] = field(default_factory=dict)
    realized_pnl: float = 0.0
    day: str = ""
    entries_today: int = 0
    pnl_today: float = 0.0

    @classmethod
    def load(cls, path: Path) -> "RunnerState":
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        st = cls(realized_pnl=float(raw.get("realized_pnl", 0.0)), day=raw.get("day", ""),
                 entries_today=int(raw.get("entries_today", 0)),
                 pnl_today=float(raw.get("pnl_today", 0.0)))
        for k, v in (raw.get("slots") or {}).items():
            st.slots[k] = Slot(**v)
        return st

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"realized_pnl": self.realized_pnl, "day": self.day,
                "entries_today": self.entries_today, "pnl_today": self.pnl_today,
                "slots": {k: asdict(s) for k, s in self.slots.items()}}
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def evaluate(spec: SleeveSpec, raw) -> dict:
    """Signals on the LAST closed bar of ``raw`` (time/open/high/low/close/volume)."""
    d = add_indicators(raw)
    i = len(d) - 1
    out = {"bar": float(d["time"].iloc[i]), "close": float(d["close"].iloc[i]),
           "warm": len(d) >= WARM_BARS, "entry": False, "exit": False, "exit_reason": ""}
    if spec.family == "regime":
        s = regime_signals(d, spec.params)
        out["entry"] = bool(s["entry"][i]) and math.isfinite(float(d["atr"].iloc[i]))
        if bool(s["chandelier_exit"][i]):
            out["exit"], out["exit_reason"] = True, "chandelier"
        elif spec.params.get("exit_on_chop", True) and not bool(s["adx_on"][i]):
            out["exit"], out["exit_reason"] = True, "regime_chop"
    elif spec.family == "meanrev":
        s = meanrev_signals(d, spec.params)
        out["entry"] = bool(s["entry"][i]) and math.isfinite(float(d["atr"].iloc[i]))
        if bool(s["exit"][i]):
            out["exit"], out["exit_reason"] = True, "reverted"
    else:
        raise ValueError(spec.family)
    if not out["warm"]:
        out["entry"] = False
    return out


class AlpacaPaperRunner:
    def __init__(self, gateway: AlpacaGateway, config: RunnerConfig,
                 *, clock: Callable[[], datetime] | None = None,
                 log: Callable[[str], None] | None = None) -> None:
        if not gateway.paper:
            raise RuntimeError("AlpacaPaperRunner only runs against the Alpaca PAPER endpoint")
        self.gw = gateway
        self.cfg = config
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.log = log or (lambda _m: None)
        self.state = RunnerState.load(config.state_path)
        for sl in config.sleeves:
            if sl not in SLEEVES:
                raise ValueError(f"unknown sleeve {sl}")
            for sym in config.symbols:
                key = f"{sl}|{sym}"
                self.state.slots.setdefault(key, Slot(sleeve=sl, symbol=sym))
        n = max(1, len(config.sleeves) * len(config.symbols))
        self.slot_cap = config.max_slot_fraction or (1.0 / n)

    # ── bookkeeping ───────────────────────────────────────
    def _event(self, kind: str, slot: Slot | None = None, **data) -> None:
        row = {"ts": _iso(self.clock().timestamp()), "event": kind, **data}
        if slot is not None:
            row.update({"sleeve": slot.sleeve, "symbol": slot.symbol})
        _append_jsonl(self.cfg.events_path, row)
        self.log(f"{kind} {row.get('sleeve', '')} {row.get('symbol', '')} "
                 + " ".join(f"{k}={v}" for k, v in data.items()))

    def _roll_day(self) -> None:
        today = self.clock().strftime("%Y-%m-%d")
        if self.state.day != today:
            self.state.day, self.state.entries_today, self.state.pnl_today = today, 0, 0.0

    def sizing_equity(self) -> float:
        return max(0.0, self.cfg.budget_usd + self.state.realized_pnl)

    def entries_blocked(self) -> str:
        if self.state.entries_today >= self.cfg.max_entries_per_day:
            return "daily entry cap"
        if self.state.pnl_today <= -self.cfg.max_daily_loss_fraction * self.cfg.budget_usd:
            return "daily loss limit"
        return ""

    def _client_id(self, slot: Slot, tag: str, bar: float) -> str:
        return f"dub-{slot.sleeve}-{slot.symbol.replace('/', '')}-{int(bar)}-{tag}"

    # ── main cycle ─────────────────────────────────────────
    def cycle(self) -> None:
        self._roll_day()
        bars_cache: dict[tuple[str, int], Any] = {}
        prices: dict[str, float] = {}

        def price(sym: str) -> float:
            if sym not in prices:
                prices[sym] = self.gw.latest_price(sym)["last"]
            return prices[sym]

        for slot in self.state.slots.values():
            if slot.sleeve not in self.cfg.sleeves or slot.symbol not in self.cfg.symbols:
                continue
            spec = SLEEVES[slot.sleeve]
            try:
                self._step(slot, spec, bars_cache, price)
            except Exception as exc:  # one bad slot never stops the others
                self._event("slot_error", slot, error=f"{type(exc).__name__}: {exc}")
            self.state.save(self.cfg.state_path)

    def _step(self, slot: Slot, spec: SleeveSpec, bars_cache: dict, price) -> None:
        now = self.clock().timestamp()
        if slot.status == "entering":
            self._check_entry(slot, spec, now)
        if slot.status == "exiting":
            self._check_exit(slot)
        if slot.status == "open":
            px = price(slot.symbol)
            if px <= slot.stop_px:
                self._exit(slot, "stop", now)
            elif px >= slot.tp_px:
                self._exit(slot, "tp", now)

        key = (slot.symbol, spec.tf)
        if key not in bars_cache:
            bars_cache[key] = self.gw.fetch_bars(slot.symbol, spec.tf, bars=FETCH_BARS)
        raw = bars_cache[key]
        if raw is None or len(raw) == 0:
            return
        last_bar = float(raw["time"].iloc[-1])
        if last_bar <= slot.last_bar:
            return
        # Stale feed guard: the last CLOSED bar should have closed within ~2 bars.
        if now - (last_bar + spec.tf * 60) > 2 * spec.tf * 60:
            self._event("stale_bars", slot, last_bar=_iso(last_bar))
            return
        sig = evaluate(spec, raw)
        slot.last_bar = last_bar
        # Exits act even when late; entries only on a bar that just closed, so a
        # restart never chases a signal that is hours old.
        fresh = now - (last_bar + spec.tf * 60) <= ENTRY_FRESH_SECONDS
        if slot.status == "open" and sig["exit"]:
            self._exit(slot, sig["exit_reason"], now)
        elif slot.status == "flat" and sig["entry"]:
            if fresh:
                self._enter(slot, spec, sig, now)
            else:
                self._event("entry_skipped", slot, reason="signal bar not fresh",
                            bar=_iso(last_bar))

    # ── entries ────────────────────────────────────────────
    def _enter(self, slot: Slot, spec: SleeveSpec, sig: dict, now: float) -> None:
        blocked = self.entries_blocked()
        if blocked:
            self._event("entry_skipped", slot, reason=blocked)
            return
        eq = self.sizing_equity()
        notional = min(eq * self.cfg.risk_per_trade / spec.stop, eq * self.slot_cap)
        try:
            notional = min(notional, self.gw.cash() * 0.98)
        except Exception:
            pass
        meta = self.gw.asset(slot.symbol)
        if spec.entry == "limit":
            limit = meta.floor_price(sig["close"] * (1 - float(spec.params.get("limit_offset", 0.001))))
            ref = float(limit)
        else:
            limit = None
            ref = float(self.gw.latest_price(slot.symbol)["last"])
        qty = meta.floor_qty(Decimal(str(notional)) / Decimal(str(ref)))
        if qty <= 0:
            self._event("entry_skipped", slot, reason="below minimum order size",
                        notional=round(notional, 2), min_qty=str(meta.min_order_size))
            return
        cid = self._client_id(slot, "e", sig["bar"])
        o = self.gw.submit_order(symbol=slot.symbol, side="buy",
                                 order_type="limit" if limit is not None else "market",
                                 qty=qty, limit_price=limit, client_order_id=cid,
                                 time_in_force="gtc")
        slot.status = "entering"
        slot.entry_order_id, slot.entry_client_id = str(o["order_id"]), cid
        slot.entry_maker = limit is not None
        slot.signal_bar = sig["bar"]
        # Limit rests for the bar after the signal bar, then expires.
        slot.entry_expires = sig["bar"] + 2 * spec.tf * 60 if limit is not None else 0.0
        self.state.entries_today += 1
        self._event("entry_submitted", slot, qty=str(qty), limit=str(limit) if limit else "market",
                    notional=round(notional, 2), order_id=slot.entry_order_id)
        self._check_entry(slot, spec, now)

    def _check_entry(self, slot: Slot, spec: SleeveSpec, now: float) -> None:
        o = self.gw.get_order(slot.entry_order_id)
        status, filled = o["status"], o["filled_qty"]
        if status == "filled":
            self._opened(slot, spec, filled, o["filled_avg_price"])
            return
        if status in OPEN_STATUSES:
            if slot.entry_expires and now >= slot.entry_expires:
                self.gw.cancel_order(slot.entry_order_id)
                o = self.gw.get_order(slot.entry_order_id)
                if o["filled_qty"] > 0:
                    self._opened(slot, spec, o["filled_qty"], o["filled_avg_price"])
                else:
                    self._event("entry_unfilled", slot, order_id=slot.entry_order_id)
                    slot.reset()
            return
        # canceled / expired / rejected outside our control
        if filled > 0:
            self._opened(slot, spec, filled, o["filled_avg_price"])
        else:
            self._event("entry_dead", slot, status=status)
            slot.reset()

    def _opened(self, slot: Slot, spec: SleeveSpec, qty: float, px: float) -> None:
        slot.status, slot.qty, slot.entry_px = "open", float(qty), float(px)
        slot.entry_time = _iso(self.clock().timestamp())
        slot.stop_px = slot.entry_px * (1 - spec.stop)
        slot.tp_px = slot.entry_px * (1 + spec.tp)
        self._event("entry_filled", slot, qty=qty, price=px, stop=round(slot.stop_px, 6),
                    tp=round(slot.tp_px, 6))

    # ── exits ──────────────────────────────────────────────
    def _exit(self, slot: Slot, reason: str, now: float) -> None:
        meta = self.gw.asset(slot.symbol)
        held = self.gw.position_qty(slot.symbol)
        others = sum(s.qty for s in self.state.slots.values()
                     if s is not slot and s.symbol == slot.symbol and s.status in ("open", "exiting"))
        # Fees are taken in coin, so this slot may own slightly less than it bought.
        # The last open lot on a symbol sells whatever is left.
        want = slot.qty if others > 0 else max(slot.qty, held)
        qty = meta.floor_qty(min(want, held))
        if qty <= 0:
            self._event("exit_no_position", slot, reason=reason, held=held)
            self._closed(slot, slot.entry_px, reason + "_missing")
            return
        # Deterministic per attempt: a crash after submit and before the state
        # save reuses the id, and submit_order returns the existing order.
        cid = self._client_id(slot, f"x{slot.exit_attempts}", slot.signal_bar)
        o = self.gw.submit_order(symbol=slot.symbol, side="sell", order_type="market",
                                 qty=qty, client_order_id=cid, time_in_force="gtc")
        slot.status, slot.exit_order_id, slot.exit_client_id = "exiting", str(o["order_id"]), cid
        slot.exit_reason = reason
        self._event("exit_submitted", slot, reason=reason, qty=str(qty), order_id=slot.exit_order_id)
        self._check_exit(slot)

    def _check_exit(self, slot: Slot) -> None:
        o = self.gw.get_order(slot.exit_order_id)
        if o["status"] == "filled":
            self._closed(slot, o["filled_avg_price"], slot.exit_reason)
        elif o["status"] not in OPEN_STATUSES:
            self._event("exit_retry", slot, status=o["status"])
            slot.exit_attempts += 1
            slot.status = "open"   # next cycle re-checks stops/signals and resubmits

    def _closed(self, slot: Slot, exit_px: float, reason: str) -> None:
        gross = exit_px / slot.entry_px - 1 if slot.entry_px > 0 else 0.0
        cost = ((self.cfg.maker_bps if slot.entry_maker else self.cfg.taker_bps)
                + self.cfg.taker_bps) / 1e4
        net = gross - cost
        pnl = slot.qty * slot.entry_px * net
        self.state.realized_pnl += pnl
        self.state.pnl_today += pnl
        _append_jsonl(self.cfg.journal_path, {
            "sleeve": slot.sleeve, "symbol": slot.symbol, "entry_time": slot.entry_time,
            "exit_time": _iso(self.clock().timestamp()), "entry_px": slot.entry_px,
            "exit_px": float(exit_px), "qty": slot.qty, "entry_maker": slot.entry_maker,
            "gross_bps": round(gross * 1e4, 2), "cost_bps": round(cost * 1e4, 2),
            "net_bps": round(net * 1e4, 2), "pnl_usd": round(pnl, 4), "reason": reason,
        })
        self._event("trade_closed", slot, reason=reason, net_bps=round(net * 1e4, 1),
                    pnl_usd=round(pnl, 4))
        slot.reset()


# ── reporting ─────────────────────────────────────────────

def report(journal_path: Path, *, min_trades: int = 30, min_pf: float = 1.10) -> list[dict]:
    """Per-sleeve forward results and whether each passes the promotion gate."""
    rows: dict[str, list[dict]] = {}
    if journal_path.exists():
        for line in journal_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                t = json.loads(line)
                rows.setdefault(t["sleeve"], []).append(t)
    out = []
    for sleeve in SLEEVES:
        tr = rows.get(sleeve, [])
        pnl = [t["pnl_usd"] for t in tr]
        gains = sum(p for p in pnl if p > 0)
        losses = -sum(p for p in pnl if p < 0)
        pf = gains / losses if losses > 0 else (math.inf if gains > 0 else 0.0)
        out.append({
            "sleeve": sleeve, "trades": len(tr),
            "win_rate": round(sum(1 for p in pnl if p > 0) / len(tr), 3) if tr else 0.0,
            "avg_net_bps": round(float(np.mean([t["net_bps"] for t in tr])), 1) if tr else 0.0,
            "pnl_usd": round(sum(pnl), 2), "profit_factor": round(pf, 2) if math.isfinite(pf) else pf,
            "passes_gate": len(tr) >= min_trades and pf >= min_pf,
        })
    return out

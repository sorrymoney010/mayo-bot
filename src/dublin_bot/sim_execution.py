"""Simulated execution on real market data: Binance.US paper trading.

Binance.US has no paper account, so this wraps a market-data gateway
(``BinanceUSGateway`` without order permission) and fills orders locally,
against real 1-minute Binance.US prices, with real fees and spreads:

* market buy  → fills at the ask + slippage; taker fee taken in coin
* market sell → fills at the bid − slippage; taker fee taken in USD
* limit buy   → fills at the limit once a 1-minute low reaches it; maker fee in
  coin. Marketable on arrival (ask ≤ limit) → fills at the ask as taker.
* stop-limit sell (``stop_limit``) → triggers when a 1-minute low reaches the
  stop. If that minute stayed above the limit it fills at the stop − slippage;
  if price gapped through the limit it rests as a limit sell and fills only
  when a later 1-minute high gets back to the limit (exactly the gap risk a
  real stop-limit has).

Coin under an open sell order is locked, like on the exchange. Everything is
persisted to a JSON file so restarts keep balances and open orders.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from .errors import BrokerError

OPEN = "new"


class SimulatedExecution:
    paper = True
    supports_resting_stop = True
    broker_name = "binanceus-sim"

    def __init__(self, data_gateway: Any, *, state_path: Path, starting_cash: float,
                 taker_bps: float, maker_bps: float, slippage_bps: float = 5.0,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.data = data_gateway
        self.settings = data_gateway.settings
        self.path = Path(state_path)
        self.taker = taker_bps / 1e4
        self.maker = maker_bps / 1e4
        self.slip = slippage_bps / 1e4
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if self.path.exists():
            self.s = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.s = {"cash": float(starting_cash), "pos": {}, "orders": {}}
            self._save()

    # ── persistence ───────────────────────────────────────
    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".sim-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.s, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    def _now(self) -> float:
        return self.clock().timestamp()

    # ── market data passthrough ───────────────────────────
    def fetch_bars(self, *a, **kw):
        return self.data.fetch_bars(*a, **kw)

    def latest_price(self, symbol: str) -> dict:
        return self.data.latest_price(symbol)

    def asset(self, symbol: str):
        return self.data.asset(symbol)

    # ── balances ──────────────────────────────────────────
    def _locked(self, symbol: str) -> float:
        return sum(float(o["qty"]) - float(o["filled_qty"]) for o in self.s["orders"].values()
                   if o["symbol"] == symbol and o["side"] == "sell" and o["status"] == OPEN)

    def cash(self) -> float:
        reserved = sum((float(o["qty"]) - float(o["filled_qty"])) * float(o["limit_price"])
                       for o in self.s["orders"].values()
                       if o["side"] == "buy" and o["status"] == OPEN)
        return self.s["cash"] - reserved

    def position_qty(self, symbol: str) -> float:
        return max(0.0, self.s["pos"].get(symbol, 0.0) - self._locked(symbol))

    def balance_total(self, symbol: str) -> float:
        return self.s["pos"].get(symbol, 0.0)

    # ── orders ────────────────────────────────────────────
    def _by_cid(self, cid: str) -> dict | None:
        for o in self.s["orders"].values():
            if o["client_order_id"] == cid:
                return o
        return None

    def get_order_by_client_id(self, client_order_id: str, *, symbol: str = "") -> dict | None:
        o = self._by_cid(client_order_id)
        return dict(o) if o else None

    def submit_order(self, *, symbol: str, side: str, order_type: str, client_order_id: str,
                     qty=None, notional=None, limit_price=None, stop_price=None,
                     time_in_force: str = "gtc") -> dict:
        existing = self._by_cid(client_order_id)
        if existing:
            return dict(existing)
        meta = self.asset(symbol)
        px = self.latest_price(symbol)
        if qty is None:
            qty = Decimal(str(notional)) / Decimal(str(px["ask"] or px["last"]))
        q = meta.floor_market_qty(qty) if order_type == "market" else meta.floor_qty(qty)
        if q <= 0:
            raise BrokerError(f"sim: {symbol} quantity {qty} below minimum {meta.min_order_size}")
        o = {"order_id": uuid.uuid4().hex[:16], "client_order_id": client_order_id, "symbol": symbol,
             "side": side, "type": order_type, "status": OPEN, "qty": float(q),
             "limit_price": float(limit_price or 0), "stop_price": float(stop_price or 0),
             "filled_qty": 0.0, "filled_avg_price": 0.0, "triggered": False,
             "created": self._now(), "checked": self._now()}
        if side == "sell" and float(q) > self.position_qty(symbol) + 1e-12:
            raise BrokerError(f"sim: insufficient {symbol} balance for sell {q}")
        if side == "buy":
            cost = float(q) * (o["limit_price"] or px["ask"] or px["last"])
            if cost > self.cash() + 1e-9:
                raise BrokerError(f"sim: insufficient USD (need {cost:.2f}, have {self.cash():.2f})")
        self.s["orders"][o["order_id"]] = o
        if order_type == "market":
            if side == "buy":
                self._fill(o, (px["ask"] or px["last"]) * (1 + self.slip), self.taker)
            else:
                self._fill(o, (px["bid"] or px["last"]) * (1 - self.slip), self.taker)
        elif order_type == "limit" and side == "buy" and px["ask"] and px["ask"] <= o["limit_price"]:
            self._fill(o, px["ask"], self.taker)          # marketable on arrival = taker
        self._save()
        return dict(o)

    def _fill(self, o: dict, price: float, fee: float) -> None:
        qty, sym = float(o["qty"]), o["symbol"]
        if o["side"] == "buy":
            self.s["cash"] -= qty * price
            self.s["pos"][sym] = self.s["pos"].get(sym, 0.0) + qty * (1 - fee)
        else:
            self.s["pos"][sym] = self.s["pos"].get(sym, 0.0) - qty
            self.s["cash"] += qty * price * (1 - fee)
        o.update(status="filled", filled_qty=qty, filled_avg_price=price, filled_at=self._now())

    def _advance(self, o: dict) -> None:
        """Bring one open order up to date against 1-minute prices since last check."""
        if o["status"] != OPEN:
            return
        since = float(o["checked"])
        now = self._now()
        if now - since < 30:
            return
        sym = o["symbol"]
        if o["type"] == "limit" and o["side"] == "buy":
            low = self.data.min_low_since(sym, since)
            if low is not None and low <= o["limit_price"]:
                self._fill(o, o["limit_price"], self.maker)
        elif o["type"] == "limit" and o["side"] == "sell":
            high = self.data.max_high_since(sym, since)
            if high is not None and high >= o["limit_price"]:
                self._fill(o, o["limit_price"], self.taker if o.get("triggered") else self.maker)
        elif o["type"] == "stop_limit" and o["side"] == "sell":
            low = self.data.min_low_since(sym, since)
            if low is not None and low <= o["stop_price"]:
                o["triggered"] = True
                if low >= o["limit_price"]:
                    self._fill(o, o["stop_price"] * (1 - self.slip), self.taker)
                else:
                    o["type"] = "limit"          # gapped through: rests as a limit sell
        o["checked"] = now

    def get_order(self, order_id: str, *, symbol: str = "") -> dict:
        o = self.s["orders"].get(order_id)
        if o is None:
            raise BrokerError(f"sim: unknown order {order_id}")
        self._advance(o)
        self._save()
        return dict(o)

    def cancel_order(self, order_id: str, *, symbol: str = "") -> bool:
        o = self.s["orders"].get(order_id)
        if o is None or o["status"] != OPEN:
            return False
        self._advance(o)
        if o["status"] == OPEN:
            o["status"] = "canceled"
        self._save()
        return True

    def health(self) -> dict:
        h = self.data.health()
        h["broker"] = self.broker_name
        return h

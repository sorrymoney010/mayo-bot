"""Binance.US spot venue adapter for the sleeve runner (paper or live).

Why a separate adapter from ``binance_gateway.py``: that one serves the Kraken-
shaped engine path and maps every USD pair to USDT. This one speaks Binance.US
natively for the runner:

* USD-quoted pairs by default (``BTC/USD`` → ``BTCUSD``); ``quote="USDT"`` opt-in
* real exchange filters (``LOT_SIZE`` / ``MARKET_LOT_SIZE``, ``PRICE_FILTER``,
  ``NOTIONAL`` / ``MIN_NOTIONAL``) applied before any order
* idempotent orders: ``newClientOrderId`` is deterministic and checked first
* a **resting stop-loss on the exchange** (``STOP_LOSS_LIMIT``), so a position is
  protected even if this machine sleeps or loses its connection
* ``/api/v3/order/test`` for a no-risk check of keys, signing and filters

Safety: ``paper`` is False for this class (it can move real money), so every
order path calls ``_require_live`` which needs ALL of ``ALLOW_LIVE_TRADING=true``,
``PAPER_TRADING=false``, ``DRY_RUN=false``, ``LIVE_RISK_ACKNOWLEDGEMENT`` and the
caller's ``allow_order_submission=True``. Paper trading on Binance.US prices
goes through ``SimulatedExecution`` (``sim_execution.py``), never this class's
order methods.

Binance charges commission in the asset received (or BNB if the account pays
fees in BNB), so after a buy the coin balance is slightly below ``executedQty``.
Sells always size from the free balance.
"""
from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from .alpaca_gateway import live_locks_open
from .audit import AuditEvent, AuditLog
from .config import Settings
from .errors import BrokerError, SafetyLockError

HOST = "https://api.binance.us"
_INTERVALS = {1: "1m", 3: "3m", 5: "5m", 15: "15m", 30: "30m", 60: "1h", 120: "2h",
              240: "4h", 360: "6h", 480: "8h", 720: "12h", 1440: "1d"}
OPEN_STATUSES = {"new", "partially_filled", "pending_cancel", "pending_new"}
_ORDER_MISSING = -2013   # "Order does not exist."
_CID_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:/")


def interval_str(minutes: int) -> str:
    try:
        return _INTERVALS[int(minutes)]
    except KeyError as exc:
        raise ValueError(f"Binance.US has no {minutes}m klines") from exc


def _q(value: Decimal, step: Decimal) -> Decimal:
    """Quantize to the step's decimal places (Binance wants '97000.00', not '97000' or '1E-5')."""
    places = max(0, -step.normalize().as_tuple().exponent)
    return value.quantize(Decimal(1).scaleb(-places))


def fmt(d: Decimal) -> str:
    return format(d, "f")


@dataclass(frozen=True)
class SymbolFilters:
    symbol: str          # canonical, e.g. BTC/USD
    pair: str            # exchange, e.g. BTCUSD
    base: str
    quote: str
    min_order_size: Decimal
    min_trade_increment: Decimal
    market_min_qty: Decimal
    market_step: Decimal
    price_increment: Decimal
    min_notional: Decimal
    min_notional_market: bool
    tradable: bool
    order_types: tuple[str, ...]

    def _floor(self, qty, step: Decimal, minimum: Decimal) -> Decimal:
        step = step if step > 0 else Decimal("1e-8")
        q = (Decimal(str(qty)) / step).to_integral_value(rounding=ROUND_DOWN) * step
        return _q(q, step) if q >= minimum else Decimal("0")

    def floor_qty(self, qty) -> Decimal:
        return self._floor(qty, self.min_trade_increment, self.min_order_size)

    def floor_market_qty(self, qty) -> Decimal:
        step = self.market_step if self.market_step > 0 else self.min_trade_increment
        minimum = max(self.market_min_qty, self.min_order_size)
        return self._floor(qty, step, minimum)

    def floor_price(self, price) -> Decimal:
        step = self.price_increment if self.price_increment > 0 else Decimal("0.01")
        return _q((Decimal(str(price)) / step).to_integral_value(rounding=ROUND_DOWN) * step, step)

    def ceil_price(self, price) -> Decimal:
        step = self.price_increment if self.price_increment > 0 else Decimal("0.01")
        return _q((Decimal(str(price)) / step).to_integral_value(rounding=ROUND_UP) * step, step)

    def notional_ok(self, qty: Decimal, price: float, *, market: bool) -> bool:
        if market and not self.min_notional_market:
            return True
        return qty * Decimal(str(price)) >= self.min_notional


class BinanceUSGateway:
    """Market data + signed spot trading on Binance.US."""

    broker_name = "binanceus"
    paper = False                 # real money; the runner gates on live locks
    supports_resting_stop = True

    def __init__(self, settings: Settings, *, audit: AuditLog | None = None,
                 session: Any | None = None, allow_order_submission: bool = False,
                 clock: Any | None = None, quote: str = "USD", recv_window_ms: int = 5000) -> None:
        self.settings = settings
        self.audit = audit or AuditLog(Path(settings.audit_log_path))
        self._session = session or requests.Session()
        self._key = settings.binance_api_key or ""
        self._secret = settings.binance_api_secret or ""
        self._allow = bool(allow_order_submission)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.quote = quote.upper()
        self.recv_window_ms = int(recv_window_ms)
        self._filters: dict[str, SymbolFilters] = {}
        self._time_offset_ms = 0

    # ── symbols ────────────────────────────────────────────
    def pair(self, symbol: str) -> str:
        base, _, _q = symbol.upper().partition("/")
        return f"{base}{self.quote}"

    # ── safety ─────────────────────────────────────────────
    @property
    def order_submission_enabled(self) -> bool:
        return self._allow and live_locks_open(self.settings) and bool(self._key and self._secret)

    def _require_live(self) -> None:
        if not self._allow:
            raise SafetyLockError("Binance.US order submission not enabled for this gateway")
        if not live_locks_open(self.settings):
            raise SafetyLockError(
                "Binance.US LIVE orders refused: set ALLOW_LIVE_TRADING=true, PAPER_TRADING=false, "
                "DRY_RUN=false and LIVE_RISK_ACKNOWLEDGEMENT"
            )
        if not (self._key and self._secret):
            raise SafetyLockError("BINANCE_API_KEY / BINANCE_API_SECRET not configured")

    # ── http ───────────────────────────────────────────────
    def _raise(self, r, method: str, path: str) -> None:
        try:
            body = r.json()
        except ValueError:
            body = {"msg": r.text}
        code = body.get("code") if isinstance(body, dict) else None
        msg = body.get("msg") if isinstance(body, dict) else str(body)
        err = BrokerError(f"Binance.US {method} {path} -> {r.status_code} {code}: {msg}")
        err.code = code  # type: ignore[attr-defined]
        raise err

    def _public(self, path: str, params: dict | None = None) -> Any:
        try:
            r = self._session.request("GET", HOST + path, params=params or {},
                                      timeout=self.settings.http_timeout_seconds)
        except requests.RequestException as exc:
            raise BrokerError(f"Binance.US GET {path} failed: {exc}") from exc
        if r.status_code >= 400:
            self._raise(r, "GET", path)
        return r.json()

    def _signed(self, method: str, path: str, params: dict | None = None) -> Any:
        if not (self._key and self._secret):
            raise SafetyLockError("BINANCE_API_KEY / BINANCE_API_SECRET not configured")
        p = {k: v for k, v in (params or {}).items() if v is not None}
        p["recvWindow"] = self.recv_window_ms
        p["timestamp"] = int(time.time() * 1000) + self._time_offset_ms
        q = urllib.parse.urlencode(p)
        sig = hmac.new(self._secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        url = f"{HOST}{path}?{q}&signature={sig}"
        try:
            r = self._session.request(method, url, headers={"X-MBX-APIKEY": self._key},
                                      timeout=self.settings.http_timeout_seconds)
        except requests.RequestException as exc:
            raise BrokerError(f"Binance.US {method} {path} failed: {exc}") from exc
        if r.status_code >= 400:
            self._raise(r, method, path)
        return r.json() if r.text else {}

    def sync_time(self) -> int:
        """Align request timestamps with the server clock (fixes -1021 errors)."""
        server = int(self._public("/api/v3/time")["serverTime"])
        self._time_offset_ms = server - int(time.time() * 1000)
        return self._time_offset_ms

    # ── market data ────────────────────────────────────────
    def fetch_bars(self, symbol: str, timeframe_minutes: int, *, bars: int = 500,
                   closed_only: bool = True, end_ms: int | None = None) -> pd.DataFrame:
        """time (epoch s, bar START), open, high, low, close, volume; closed bars only."""
        interval = interval_str(timeframe_minutes)
        step_ms = timeframe_minutes * 60_000
        now_ms = int(self._clock().timestamp() * 1000)
        end = end_ms or now_ms
        start = end - step_ms * (bars + 2)
        rows: list[list] = []
        cursor = start
        for _ in range(200):
            batch = self._public("/api/v3/klines", {"symbol": self.pair(symbol), "interval": interval,
                                                    "startTime": cursor, "endTime": end, "limit": 1000})
            if not batch:
                break
            rows.extend(batch)
            last_open = int(batch[-1][0])
            if len(batch) < 1000 or last_open + step_ms > end:
                break
            cursor = last_open + step_ms
        df = pd.DataFrame(
            [{"time": int(k[0]) / 1000.0, "open": float(k[1]), "high": float(k[2]),
              "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
              "close_ms": int(k[6])} for k in rows],
            columns=["time", "open", "high", "low", "close", "volume", "close_ms"],
        )
        if df.empty:
            return df.drop(columns="close_ms")
        df = df.drop_duplicates("time").sort_values("time").reset_index(drop=True)
        if closed_only:
            df = df[df["close_ms"] < now_ms].reset_index(drop=True)
        return df.drop(columns="close_ms").tail(bars).reset_index(drop=True)

    def min_low_since(self, symbol: str, since_ts: float) -> float | None:
        """Lowest 1-minute low since ``since_ts`` (used by simulated fills)."""
        df = self._public("/api/v3/klines", {"symbol": self.pair(symbol), "interval": "1m",
                                             "startTime": int(since_ts * 1000), "limit": 1000})
        lows = [float(k[3]) for k in df or []]
        return min(lows) if lows else None

    def max_high_since(self, symbol: str, since_ts: float) -> float | None:
        df = self._public("/api/v3/klines", {"symbol": self.pair(symbol), "interval": "1m",
                                             "startTime": int(since_ts * 1000), "limit": 1000})
        highs = [float(k[2]) for k in df or []]
        return max(highs) if highs else None

    def latest_price(self, symbol: str) -> dict:
        pair = self.pair(symbol)
        book = self._public("/api/v3/ticker/bookTicker", {"symbol": pair})
        last = float(self._public("/api/v3/ticker/price", {"symbol": pair})["price"])
        bid, ask = float(book.get("bidPrice") or 0), float(book.get("askPrice") or 0)
        if last <= 0:
            raise BrokerError(f"Binance.US returned no price for {pair}")
        return {"symbol": symbol, "bid": bid, "ask": ask, "last": last}

    def asset(self, symbol: str) -> SymbolFilters:
        pair = self.pair(symbol)
        if pair in self._filters:
            return self._filters[pair]
        info = self._public("/api/v3/exchangeInfo", {"symbol": pair})
        syms = info.get("symbols") or []
        if not syms:
            raise BrokerError(f"Binance.US does not list {pair}")
        s = syms[0]
        f = {x["filterType"]: x for x in s.get("filters", [])}
        lot = f.get("LOT_SIZE", {})
        mlot = f.get("MARKET_LOT_SIZE", {})
        price = f.get("PRICE_FILTER", {})
        notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
        apply_mkt = notional.get("applyMinToMarket", notional.get("applyToMarket", True))
        meta = SymbolFilters(
            symbol=symbol, pair=pair, base=s.get("baseAsset", ""), quote=s.get("quoteAsset", self.quote),
            min_order_size=Decimal(str(lot.get("minQty", "0"))),
            min_trade_increment=Decimal(str(lot.get("stepSize", "0.00000001"))),
            market_min_qty=Decimal(str(mlot.get("minQty", lot.get("minQty", "0")))),
            market_step=Decimal(str(mlot.get("stepSize", "0")) or "0"),
            price_increment=Decimal(str(price.get("tickSize", "0.01"))),
            min_notional=Decimal(str(notional.get("minNotional", "0"))),
            min_notional_market=bool(apply_mkt),
            tradable=s.get("status") == "TRADING",
            order_types=tuple(s.get("orderTypes", [])),
        )
        self._filters[pair] = meta
        return meta

    # ── account ────────────────────────────────────────────
    def account(self) -> dict:
        return self._signed("GET", "/api/v3/account")

    def _balance(self, asset: str) -> tuple[float, float]:
        for b in self.account().get("balances", []):
            if b.get("asset") == asset:
                return float(b.get("free") or 0), float(b.get("locked") or 0)
        return 0.0, 0.0

    def cash(self) -> float:
        return self._balance(self.quote)[0]

    def position_qty(self, symbol: str) -> float:
        """Free coin balance (what a market sell can use right now)."""
        return self._balance(self.asset(symbol).base)[0]

    def balance_total(self, symbol: str) -> float:
        free, locked = self._balance(self.asset(symbol).base)
        return free + locked

    def commission_rates(self) -> dict:
        a = self.account()
        cr = a.get("commissionRates") or {}
        if cr:
            return {"maker_bps": float(cr.get("maker", 0)) * 1e4, "taker_bps": float(cr.get("taker", 0)) * 1e4}
        # legacy integer fields are already in basis points (10 = 0.10%)
        return {"maker_bps": float(a.get("makerCommission", 0)), "taker_bps": float(a.get("takerCommission", 0))}

    # ── orders ─────────────────────────────────────────────
    @staticmethod
    def _norm(o: dict, symbol: str = "") -> dict:
        exe = float(o.get("executedQty") or 0)
        quote = float(o.get("cummulativeQuoteQty") or 0)
        return {
            "order_id": str(o.get("orderId", "")),
            "client_order_id": o.get("clientOrderId") or o.get("origClientOrderId"),
            "symbol": symbol, "side": (o.get("side") or "").lower(),
            "type": (o.get("type") or "").lower(),
            "status": (o.get("status") or "").lower(),
            "qty": float(o.get("origQty") or 0),
            "limit_price": float(o.get("price") or 0),
            "stop_price": float(o.get("stopPrice") or 0),
            "filled_qty": exe,
            "filled_avg_price": (quote / exe) if exe > 0 else 0.0,
        }

    def get_order(self, order_id: str, *, symbol: str) -> dict:
        o = self._signed("GET", "/api/v3/order", {"symbol": self.pair(symbol), "orderId": order_id})
        return self._norm(o, symbol)

    def get_order_by_client_id(self, client_order_id: str, *, symbol: str) -> dict | None:
        try:
            o = self._signed("GET", "/api/v3/order",
                             {"symbol": self.pair(symbol), "origClientOrderId": client_order_id})
        except BrokerError as exc:
            if getattr(exc, "code", None) == _ORDER_MISSING:
                return None
            raise
        return self._norm(o, symbol)

    def open_orders(self, symbol: str | None = None) -> list[dict]:
        params = {"symbol": self.pair(symbol)} if symbol else {}
        return [self._norm(o, symbol or "") for o in self._signed("GET", "/api/v3/openOrders", params)]

    @staticmethod
    def check_client_id(cid: str) -> None:
        if not 1 <= len(cid) <= 36 or set(cid) - _CID_OK:
            raise ValueError(f"invalid Binance client order id {cid!r} (1-36 chars, [A-Za-z0-9-_.:/])")

    def _order_params(self, *, symbol: str, side: str, order_type: str, qty=None,
                      limit_price=None, stop_price=None, time_in_force: str = "gtc",
                      client_order_id: str) -> dict:
        self.check_client_id(client_order_id)
        meta = self.asset(symbol)
        side_u = side.upper()
        if side_u not in ("BUY", "SELL"):
            raise ValueError(side)
        p: dict[str, Any] = {"symbol": meta.pair, "side": side_u, "newClientOrderId": client_order_id,
                             "newOrderRespType": "FULL"}
        if qty is None:
            raise ValueError("qty required")
        if order_type == "market":
            q = meta.floor_market_qty(qty)
            p.update(type="MARKET", quantity=fmt(q))
        elif order_type == "limit":
            q = meta.floor_qty(qty)
            p.update(type="LIMIT", timeInForce=time_in_force.upper(), quantity=fmt(q),
                     price=fmt(meta.floor_price(limit_price) if side_u == "BUY" else meta.ceil_price(limit_price)))
        elif order_type == "stop_limit":
            q = meta.floor_qty(qty)
            p.update(type="STOP_LOSS_LIMIT", timeInForce="GTC", quantity=fmt(q),
                     stopPrice=fmt(meta.floor_price(stop_price)), price=fmt(meta.floor_price(limit_price)))
        else:
            raise ValueError(f"unsupported order type {order_type}")
        if Decimal(p["quantity"]) <= 0:
            raise ValueError(f"{symbol}: quantity {qty} below exchange minimum {meta.min_order_size}")
        ref = float(p.get("price") or self.latest_price(symbol)["last"])
        if not meta.notional_ok(Decimal(p["quantity"]), ref, market=order_type == "market"):
            raise ValueError(f"{symbol}: notional below exchange minimum ${meta.min_notional}")
        return p

    def submit_order(self, *, symbol: str, side: str, order_type: str, client_order_id: str,
                     qty=None, notional=None, limit_price=None, stop_price=None,
                     time_in_force: str = "gtc") -> dict:
        self._require_live()
        if notional is not None and qty is None:
            px = self.latest_price(symbol)["ask" if side == "buy" else "bid"] or self.latest_price(symbol)["last"]
            qty = Decimal(str(notional)) / Decimal(str(px))
        params = self._order_params(symbol=symbol, side=side, order_type=order_type, qty=qty,
                                    limit_price=limit_price, stop_price=stop_price,
                                    time_in_force=time_in_force, client_order_id=client_order_id)
        existing = self.get_order_by_client_id(client_order_id, symbol=symbol)
        if existing:
            self.audit.record(AuditEvent.ORDER_DUPLICATE_BLOCKED,
                              {"broker": "binanceus", "client_order_id": client_order_id})
            return existing
        self.audit.record(AuditEvent.ORDER_INTENT, {"broker": "binanceus", **params})
        try:
            o = self._signed("POST", "/api/v3/order", params)
        except BrokerError as exc:
            self.audit.record(AuditEvent.ORDER_REJECTED, {"broker": "binanceus", "client_order_id": client_order_id,
                                                          "error": str(exc)}, severity="error")
            raise
        self.audit.record(AuditEvent.ORDER_SUBMITTED, {"broker": "binanceus", "order_id": o.get("orderId"),
                                                       "client_order_id": client_order_id})
        return self._norm(o, symbol)

    def test_order(self, *, symbol: str, side: str, order_type: str, qty, client_order_id: str,
                   limit_price=None, stop_price=None) -> dict:
        """POST /api/v3/order/test: validates keys, signature and filters; places nothing."""
        params = self._order_params(symbol=symbol, side=side, order_type=order_type, qty=qty,
                                    limit_price=limit_price, stop_price=stop_price,
                                    client_order_id=client_order_id)
        self._signed("POST", "/api/v3/order/test", params)
        return params

    def cancel_order(self, order_id: str, *, symbol: str) -> bool:
        self._require_live()
        try:
            self._signed("DELETE", "/api/v3/order", {"symbol": self.pair(symbol), "orderId": order_id})
        except BrokerError as exc:
            self.audit.record(AuditEvent.ORDER_REJECTED, {"broker": "binanceus", "cancel": order_id,
                                                          "error": str(exc)}, severity="warning")
            return False
        self.audit.record(AuditEvent.ORDER_CANCELLED, {"broker": "binanceus", "order_id": order_id})
        return True

    def health(self) -> dict:
        t0 = time.time()
        try:
            self._public("/api/v3/ping")
            return {"broker": "binanceus", "reachable": True, "error": None,
                    "latency_ms": round((time.time() - t0) * 1000, 1)}
        except BrokerError as exc:
            return {"broker": "binanceus", "reachable": False, "error": str(exc)}


def history_window(days: int, now: datetime | None = None) -> tuple[int, int]:
    now = now or datetime.now(timezone.utc)
    return int((now - timedelta(days=days)).timestamp() * 1000), int(now.timestamp() * 1000)

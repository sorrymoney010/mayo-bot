"""Alpaca Crypto gateway: spot, long-only, implements ``BrokerGateway``.

Two trading hosts, chosen by ``settings.alpaca_paper`` and never by URL:

* paper (default): ``https://paper-api.alpaca.markets``, simulated money. Orders
  are allowed when the caller passes ``allow_order_submission=True`` (the
  Alpaca paper runner does; ``build_gateway`` only does when the live locks
  are open, same as Kraken and Binance).
* live: ``https://api.alpaca.markets``. Orders additionally need every live
  lock: ``ALLOW_LIVE_TRADING=true``, ``PAPER_TRADING=false``,
  ``DRY_RUN=false`` and ``LIVE_RISK_ACKNOWLEDGEMENT``. Otherwise every order
  call raises ``SafetyLockError`` before touching the network.

Market data comes from ``https://data.alpaca.markets/v1beta3/crypto/us`` (keys
optional). Alpaca crypto facts this adapter relies on: market / limit /
stop_limit orders; ``gtc`` and ``ioc`` only; no margin, no shorting; fees are
charged in the asset you receive, so a buy leaves slightly less coin than
``filled_qty`` and sells must use the position Alpaca actually holds.
"""
from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from .audit import AuditEvent, AuditLog
from .config import Settings
from .errors import BrokerError, SafetyLockError, StaleDataError
from .freshness import FreshnessVerdict

PAPER_HOST = "https://paper-api.alpaca.markets"
LIVE_HOST = "https://api.alpaca.markets"
DATA_HOST = "https://data.alpaca.markets"
_LIVE_ACK = "I_ACCEPT_LIVE_TRADING_RISK"

_TIMEFRAMES = {1: "1Min", 5: "5Min", 15: "15Min", 30: "30Min", 60: "1Hour",
               120: "2Hour", 240: "4Hour", 360: "6Hour", 720: "12Hour", 1440: "1Day"}

OPEN_STATUSES = {"new", "accepted", "pending_new", "partially_filled", "accepted_for_bidding",
                 "pending_replace", "pending_cancel", "calculated", "held"}


def live_locks_open(settings: Settings) -> bool:
    """True only when every live lock is explicitly open."""
    return bool(
        settings.allow_live_trading
        and not settings.paper_trading
        and not settings.dry_run
        and settings.live_risk_acknowledgement == _LIVE_ACK
    )


def to_alpaca(symbol: str) -> str:
    """Canonical 'BTC/USD' (or 'BTCUSD') -> Alpaca order symbol 'BTC/USD'."""
    s = symbol.upper().strip()
    if "/" in s:
        return s
    for q in ("USDT", "USDC", "USD", "BTC"):
        if s.endswith(q) and len(s) > len(q):
            return f"{s[: -len(q)]}/{q}"
    return s


def compact(symbol: str) -> str:
    """'BTC/USD' -> 'BTCUSD' (positions are returned in this form)."""
    return symbol.upper().replace("/", "")


def timeframe_str(minutes: int) -> str:
    try:
        return _TIMEFRAMES[int(minutes)]
    except KeyError as exc:
        raise ValueError(f"Alpaca has no {minutes}m bars; use one of {sorted(_TIMEFRAMES)}") from exc


@dataclass(frozen=True)
class AssetMeta:
    symbol: str
    min_order_size: Decimal
    min_trade_increment: Decimal
    price_increment: Decimal
    tradable: bool

    def floor_qty(self, qty: float | Decimal) -> Decimal:
        step = self.min_trade_increment if self.min_trade_increment > 0 else Decimal("1e-9")
        q = (Decimal(str(qty)) / step).to_integral_value(rounding=ROUND_DOWN) * step
        return q if q >= self.min_order_size else Decimal("0")

    def floor_price(self, price: float | Decimal) -> Decimal:
        step = self.price_increment if self.price_increment > 0 else Decimal("0.01")
        return (Decimal(str(price)) / step).to_integral_value(rounding=ROUND_DOWN) * step


class AlpacaGateway:
    """Alpaca Crypto spot adapter (paper by default)."""

    broker_name = "alpaca"

    def __init__(
        self,
        settings: Settings,
        *,
        audit: AuditLog | None = None,
        session: Any | None = None,
        allow_order_submission: bool = False,
        clock: Any | None = None,
    ) -> None:
        self.settings = settings
        self.audit = audit or AuditLog(Path(settings.audit_log_path))
        self._session = session or requests.Session()
        self.paper = bool(settings.alpaca_paper)
        self.host = PAPER_HOST if self.paper else LIVE_HOST
        self._key = settings.alpaca_api_key or ""
        self._secret = settings.alpaca_api_secret or ""
        self._allow_order_submission = bool(allow_order_submission)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._assets: dict[str, AssetMeta] = {}

    # ── safety ─────────────────────────────────────────────
    @property
    def order_submission_enabled(self) -> bool:
        if not self._allow_order_submission:
            return False
        if self.paper:
            return True
        return live_locks_open(self.settings)

    def _require_orders(self) -> None:
        if not self._allow_order_submission:
            raise SafetyLockError("Alpaca order submission not enabled for this gateway")
        if not self.paper and not live_locks_open(self.settings):
            raise SafetyLockError(
                "Alpaca LIVE orders refused: set ALLOW_LIVE_TRADING=true, PAPER_TRADING=false, "
                "DRY_RUN=false and LIVE_RISK_ACKNOWLEDGEMENT, or keep ALPACA_PAPER=true"
            )
        if not (self._key and self._secret):
            raise SafetyLockError("Alpaca API key/secret not configured")

    # ── http ───────────────────────────────────────────────
    def _headers(self) -> dict:
        h = {"Accept": "application/json"}
        if self._key and self._secret:
            h["APCA-API-KEY-ID"] = self._key
            h["APCA-API-SECRET-KEY"] = self._secret
        return h

    def _request(self, method: str, url: str, *, params: dict | None = None,
                 json_body: dict | None = None, ok_404: bool = False) -> Any:
        try:
            r = self._session.request(method, url, params=params, json=json_body,
                                      headers=self._headers(),
                                      timeout=self.settings.http_timeout_seconds)
        except requests.RequestException as exc:
            raise BrokerError(f"Alpaca {method} {url} failed: {exc}") from exc
        if ok_404 and r.status_code == 404:
            return None
        if r.status_code == 204:
            return {}
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", r.text)
            except ValueError:
                msg = r.text
            raise BrokerError(f"Alpaca {method} {url} -> {r.status_code}: {msg}")
        try:
            return r.json()
        except ValueError:
            return {}

    def _trading(self, method: str, path: str, **kw) -> Any:
        return self._request(method, f"{self.host}{path}", **kw)

    def _data(self, path: str, params: dict) -> Any:
        return self._request("GET", f"{DATA_HOST}/v1beta3/crypto/us{path}", params=params)

    # ── market data ────────────────────────────────────────
    def fetch_bars(self, symbol: str, timeframe_minutes: int, *, bars: int = 500,
                   closed_only: bool = True) -> pd.DataFrame:
        """OHLCV frame with columns time (epoch s, bar START), open, high, low, close, volume.

        Pages through Alpaca until ``bars`` rows are collected. When
        ``closed_only`` the still-forming last bar is dropped, so signals only
        ever see closed bars (same as the backtester).
        """
        sym = to_alpaca(symbol)
        tf = timeframe_str(timeframe_minutes)
        now = self._clock()
        start = now - timedelta(minutes=timeframe_minutes * (bars + 5))
        params: dict[str, Any] = {
            "symbols": sym, "timeframe": tf,
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": 10000, "sort": "asc",
        }
        rows: list[dict] = []
        for _ in range(50):
            data = self._data("/bars", params) or {}
            for b in (data.get("bars") or {}).get(sym, []) or []:
                rows.append({
                    "time": pd.Timestamp(b["t"]).timestamp(),
                    "open": float(b["o"]), "high": float(b["h"]), "low": float(b["l"]),
                    "close": float(b["c"]), "volume": float(b.get("v", 0.0)),
                })
            token = data.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
        if df.empty:
            return df
        df = df.drop_duplicates("time").sort_values("time").reset_index(drop=True)
        if closed_only:
            cutoff = now.timestamp() - timeframe_minutes * 60
            df = df[df["time"] <= cutoff].reset_index(drop=True)
        return df.tail(bars).reset_index(drop=True)

    def latest_price(self, symbol: str) -> dict:
        sym = to_alpaca(symbol)
        q = (self._data("/latest/quotes", {"symbols": sym}).get("quotes") or {}).get(sym) or {}
        t = (self._data("/latest/trades", {"symbols": sym}).get("trades") or {}).get(sym) or {}
        bid, ask, last = float(q.get("bp") or 0), float(q.get("ap") or 0), float(t.get("p") or 0)
        if last <= 0 and bid > 0 and ask > 0:
            last = (bid + ask) / 2
        if last <= 0:
            raise BrokerError(f"Alpaca returned no price for {sym}")
        return {"symbol": sym, "bid": bid, "ask": ask, "last": last}

    # BrokerGateway protocol (single configured symbol, engine-compatible)
    def get_bars(self, *, validate: bool = True) -> pd.DataFrame:
        df = self.fetch_bars(self.settings.symbol, self.settings.timeframe_minutes,
                             bars=min(self.settings.lookback_bars, 1000))
        if df.empty:
            return df
        out = df.copy()
        out["time"] = pd.to_datetime(out["time"], unit="s", utc=True)
        out = out.set_index("time").sort_index()
        if validate:
            verdict = self.check_freshness(out)
            if not verdict.fresh:
                raise StaleDataError(verdict.reason)
        return out

    def check_freshness(self, bars: pd.DataFrame | None = None) -> FreshnessVerdict:
        max_age = self.settings.timeframe_minutes * self.settings.max_bar_age_multiple
        if bars is None or len(bars) == 0:
            return FreshnessVerdict(fresh=False, reason="no bars", bar_age_minutes=1e9,
                                    max_age_minutes=max_age)
        last = bars.index[-1]
        if getattr(last, "tzinfo", None) is None:
            last = pd.Timestamp(last).tz_localize("UTC")
        age = (pd.Timestamp(self._clock()) - last).total_seconds() / 60.0
        return FreshnessVerdict(fresh=age <= max_age, reason=f"bar age {age:.1f}m",
                                bar_age_minutes=age, max_age_minutes=max_age,
                                last_bar_timestamp=str(last))

    def market_quality(self) -> dict:
        p = self.latest_price(self.settings.symbol)
        return {"bid": p["bid"], "ask": p["ask"], "last": p["last"]}

    def asset(self, symbol: str) -> AssetMeta:
        sym = to_alpaca(symbol)
        if sym not in self._assets:
            a = self._trading("GET", "/v2/assets/" + urllib.parse.quote(sym, safe=""))
            self._assets[sym] = AssetMeta(
                symbol=sym,
                min_order_size=Decimal(str(a.get("min_order_size") or "0")),
                min_trade_increment=Decimal(str(a.get("min_trade_increment") or "0.00000001")),
                price_increment=Decimal(str(a.get("price_increment") or "0.01")),
                tradable=bool(a.get("tradable", True)),
            )
        return self._assets[sym]

    # ── account ────────────────────────────────────────────
    def account(self) -> dict:
        if not (self._key and self._secret):
            raise SafetyLockError("Alpaca API key/secret not configured")
        return self._trading("GET", "/v2/account")

    def account_equity(self) -> float:
        if not (self._key and self._secret):
            return float(self.settings.strategy_equity_usd)
        return float(self.account().get("equity") or 0.0)

    def cash(self) -> float:
        a = self.account()
        return float(a.get("non_marginable_buying_power") or a.get("cash") or 0.0)

    def positions(self) -> list[dict]:
        if not (self._key and self._secret):
            return []
        out = []
        for p in self._trading("GET", "/v2/positions") or []:
            if p.get("asset_class") not in (None, "crypto"):
                continue
            out.append({
                "symbol": to_alpaca(p["symbol"]),
                "quantity": float(p.get("qty") or 0.0),
                "qty_available": float(p.get("qty_available") or p.get("qty") or 0.0),
                "market_value": float(p.get("market_value") or 0.0),
                "average_entry": float(p.get("avg_entry_price") or 0.0),
            })
        return out

    def position_qty(self, symbol: str) -> float:
        """Coin Alpaca actually holds and can sell (fees already deducted)."""
        if not (self._key and self._secret):
            return 0.0
        p = self._trading("GET", "/v2/positions/" + compact(symbol), ok_404=True)
        if not p:
            return 0.0
        return float(p.get("qty_available") or p.get("qty") or 0.0)

    def has_position(self) -> bool:
        return self.position_qty(self.settings.symbol) > 0

    # ── orders ─────────────────────────────────────────────
    def orders(self, symbol: str | None = None) -> list[dict]:
        if not (self._key and self._secret):
            return []
        params: dict[str, Any] = {"status": "open", "limit": 500}
        if symbol:
            params["symbols"] = to_alpaca(symbol)
        return [self._norm_order(o) for o in self._trading("GET", "/v2/orders", params=params) or []]

    def get_order(self, order_id: str, *, symbol: str = "") -> dict:
        return self._norm_order(self._trading("GET", f"/v2/orders/{order_id}"))

    def get_order_by_client_id(self, client_order_id: str) -> dict | None:
        if not (self._key and self._secret):
            return None
        o = self._trading("GET", "/v2/orders:by_client_order_id",
                          params={"client_order_id": client_order_id}, ok_404=True)
        return self._norm_order(o) if o else None

    def find_order_by_userref(self, userref: int) -> dict | None:
        return self.get_order_by_client_id(f"dublin-{userref}")

    @staticmethod
    def _norm_order(o: dict) -> dict:
        def f(k: str) -> float:
            v = o.get(k)
            return float(v) if v not in (None, "") else 0.0
        return {
            "order_id": o.get("id"),
            "client_order_id": o.get("client_order_id"),
            "symbol": to_alpaca(o.get("symbol") or ""),
            "side": o.get("side"),
            "type": o.get("type") or o.get("order_type"),
            "status": (o.get("status") or "").lower(),
            "qty": f("qty"),
            "notional": f("notional"),
            "limit_price": f("limit_price"),
            "filled_qty": f("filled_qty"),
            "filled_avg_price": f("filled_avg_price"),
            "submitted_at": o.get("submitted_at"),
            "filled_at": o.get("filled_at"),
        }

    def submit_order(self, *, symbol: str, side: str, order_type: str,
                     client_order_id: str, qty: Decimal | float | None = None,
                     notional: float | None = None, limit_price: Decimal | float | None = None,
                     time_in_force: str = "gtc") -> dict:
        """Submit one order. Idempotent on ``client_order_id``: if Alpaca already
        has an order with that id, it is returned instead of a second order."""
        self._require_orders()
        if side not in ("buy", "sell") or order_type not in ("market", "limit"):
            raise ValueError(f"unsupported order {side}/{order_type}")
        if time_in_force not in ("gtc", "ioc"):
            raise ValueError("Alpaca crypto supports only gtc and ioc")
        if len(client_order_id) > 128:
            raise ValueError("client_order_id longer than 128 characters")
        existing = self.get_order_by_client_id(client_order_id)
        if existing:
            self.audit.record(AuditEvent.ORDER_DUPLICATE_BLOCKED,
                              {"broker": "alpaca", "client_order_id": client_order_id})
            return existing
        body: dict[str, Any] = {
            "symbol": to_alpaca(symbol), "side": side, "type": order_type,
            "time_in_force": time_in_force, "client_order_id": client_order_id,
        }
        if (qty is None) == (notional is None):
            raise ValueError("pass exactly one of qty or notional")
        if qty is not None:
            body["qty"] = str(qty)
        else:
            body["notional"] = f"{float(notional):.2f}"
        if order_type == "limit":
            if limit_price is None:
                raise ValueError("limit order needs limit_price")
            body["limit_price"] = str(limit_price)
        self.audit.record(AuditEvent.ORDER_INTENT, {"broker": "alpaca", "paper": self.paper, **body})
        try:
            o = self._trading("POST", "/v2/orders", json_body=body)
        except BrokerError as exc:
            self.audit.record(AuditEvent.ORDER_REJECTED,
                              {"broker": "alpaca", "client_order_id": client_order_id,
                               "error": str(exc)}, severity="error")
            raise
        self.audit.record(AuditEvent.ORDER_SUBMITTED,
                          {"broker": "alpaca", "paper": self.paper, "order_id": o.get("id"),
                           "client_order_id": client_order_id})
        return self._norm_order(o)

    def cancel_order(self, order_id: str, *, symbol: str = "") -> bool:
        self._require_orders()
        try:
            self._trading("DELETE", f"/v2/orders/{order_id}")
        except BrokerError:
            return False
        self.audit.record(AuditEvent.ORDER_CANCELLED, {"broker": "alpaca", "order_id": order_id})
        return True

    # BrokerGateway protocol helpers
    def buy_notional(self, notional_usd: float, *, userref: int | None = None) -> str:
        if not self.order_submission_enabled:
            self.audit.record(AuditEvent.ORDER_INTENT, {"broker": "alpaca", "side": "buy",
                                                        "notional_usd": notional_usd, "dry": True})
            return f"alpaca-dry-buy-{userref}"
        o = self.submit_order(symbol=self.settings.symbol, side="buy", order_type="market",
                              notional=notional_usd,
                              client_order_id=f"dublin-{userref or int(time.time())}")
        return str(o["order_id"])

    def close_position(self, *, userref: int | None = None) -> str:
        if not self.order_submission_enabled:
            self.audit.record(AuditEvent.ORDER_INTENT, {"broker": "alpaca", "side": "sell", "dry": True})
            return f"alpaca-dry-sell-{userref}"
        qty = self.asset(self.settings.symbol).floor_qty(self.position_qty(self.settings.symbol))
        if qty <= 0:
            return "alpaca-no-position"
        o = self.submit_order(symbol=self.settings.symbol, side="sell", order_type="market",
                              qty=qty, client_order_id=f"dublin-x-{userref or int(time.time())}")
        return str(o["order_id"])

    def margin_positions(self) -> list[dict]:
        return []  # Alpaca crypto: no margin, no shorts

    # ── health ─────────────────────────────────────────────
    def health(self, *, include_freshness: bool = True) -> dict:
        t0 = time.time()
        try:
            self._trading("GET", "/v2/clock")
            return {"broker": "alpaca", "paper": self.paper, "reachable": True, "error": None,
                    "latency_ms": round((time.time() - t0) * 1000, 1)}
        except BrokerError as exc:
            return {"broker": "alpaca", "paper": self.paper, "reachable": False, "error": str(exc)}

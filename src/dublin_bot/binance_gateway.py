"""Binance Spot gateway — implements the shared ``BrokerGateway`` protocol.

Drop-in peer of ``kraken_gateway``: the engine, risk manager, dashboard and
audit log are broker-agnostic, so adding Binance is purely this module plus a
``build_gateway`` branch.  Market data uses Binance public REST (no key).  Order
submission is gated behind the same dry_run / paper_trading / allow_live_trading
flags and an explicit ``allow_order_submission`` constructor flag, exactly like
Kraken.

Symbol convention: our canonical ``"BTC/USD"`` maps to Binance ``"BTCUSDT"``
(Binance lists USD pairs as USDT).  ``resolve_symbol`` performs that mapping and
returns the same ``SymbolMeta`` shape the rest of the code expects.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from .audit import AuditEvent, AuditLog
from .config import Settings
from .errors import BrokerError, SafetyLockError, StaleDataError
from .freshness import FreshnessVerdict
from .kraken_gateway import SymbolMeta
from .precision import SizedOrder as _SizedOrder
from decimal import Decimal

_BINANCE_DEFAULT_HOST = "https://api.binance.us"  # US-compliant; set BINANCE_HOST for .com
_KNOWN_HOSTS = {
    "binance.us": "https://api.binance.us",
    "binance.com": "https://api.binance.com",
    "binance": "https://api.binance.us",
}


def _host_for(settings: Settings) -> str:
    env_host = getattr(settings, "binance_host", "") or ""
    if env_host:
        return _KNOWN_HOSTS.get(env_host.lower(), env_host.rstrip("/"))
    return _BINANCE_DEFAULT_HOST


class BinanceGateway:
    """Binance Spot adapter implementing the broker-agnostic protocol."""

    def __init__(
        self,
        settings: Settings,
        *,
        audit: AuditLog | None = None,
        session: Any | None = None,
        allow_order_submission: bool = False,
        sleep_fn: Any | None = None,
    ) -> None:
        self.settings = settings
        self.audit = audit or AuditLog(Path(settings.audit_log_path))
        self._session = session or requests.Session()
        self._host = _host_for(settings)
        self._key = settings.binance_api_key or ""
        self._secret = settings.binance_api_secret or ""
        self._allow_order_submission = bool(allow_order_submission)
        self._sleep_fn = sleep_fn or (lambda _s: None)
        self._ticker_cache: dict[str, dict] = {}

    # ── helpers ────────────────────────────────────────────
    def _public(self, path: str, params: dict | None = None) -> dict:
        url = f"{self._host}{path}"
        try:
            r = self._session.get(url, params=params or {}, timeout=self.settings.http_timeout_seconds)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            raise BrokerError(f"Binance public GET {path} failed: {exc}") from exc

    def _signed(self, path: str, params: dict | None = None, *, method: str = "GET") -> dict:
        if not (self._key and self._secret):
            raise SafetyLockError("Binance credentials not configured")
        params = dict(params or {})
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        q = urllib.parse.urlencode(params)
        sig = hmac.new(self._secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        q += f"&signature={sig}"
        url = f"{self._host}{path}"
        headers = {"X-MBX-APIKEY": self._key}
        try:
            if method == "GET":
                r = self._session.get(url + "?" + q, headers=headers, timeout=self.settings.http_timeout_seconds)
            else:
                r = self._session.post(url, data=q, headers=headers, timeout=self.settings.http_timeout_seconds)
            data = r.json()
            if isinstance(data, dict) and data.get("code") is not None and data.get("code") != 0:
                raise BrokerError(f"Binance error {data.get('code')}: {data.get('msg')}")
            return data
        except requests.RequestException as exc:
            raise BrokerError(f"Binance signed {method} {path} failed: {exc}") from exc

    @staticmethod
    def _to_binance(symbol: str) -> str:
        """'BTC/USD' -> 'BTCUSDT'."""
        base, _, quote = symbol.partition("/")
        quote = "USDT" if quote.upper() in ("USD", "USDT", "USDC") else quote.upper()
        return f"{base.upper()}{quote}"

    @staticmethod
    def _from_binance(symbol: str) -> str:
        if symbol.endswith("USDT") or symbol.endswith("USDC"):
            return f"{symbol[:-4]}/USD"
        if symbol.endswith("USD"):
            return f"{symbol[:-3]}/USD"
        return symbol

    # ── market data ────────────────────────────────────────
    def resolve_symbol(self, symbol: str | None = None) -> SymbolMeta:
        sym = symbol or self.settings.symbol
        pair = self._to_binance(sym)
        info = self._public("/api/v3/exchangeInfo", {"symbol": pair})
        if not info.get("symbols"):
            raise BrokerError(f"Binance has no symbol {pair}")
        s = info["symbols"][0]
        filt = {f["filterType"]: f for f in s.get("filters", [])}
        lot = filt.get("LOT_SIZE", {})
        price_f = filt.get("PRICE_FILTER", {})
        notional_f = filt.get("NOTIONAL", filt.get("MIN_NOTIONAL", {}))
        base = s["baseAsset"]
        quote = s["quoteAsset"]
        return SymbolMeta(
            key=pair,
            altname=pair,
            wsname=pair,
            base=base,
            quote=quote,
            lot_decimals=int(str(lot.get("stepSize", "1")).rstrip("0").split(".")[-1][:1] or 0) if "." in lot.get("stepSize", "1") else 0,
            pair_decimals=int(str(price_f.get("tickSize", "0.01")).rstrip("0").split(".")[-1][:1] or 0) if "." in str(price_f.get("tickSize", "0.01")) else 0,
            order_min=__import__("decimal").Decimal(str(lot.get("minQty", 0))),
            cost_min=__import__("decimal").Decimal(str(notional_f.get("minNotional", 0) or 0)),
            status="online",
        )

    def get_bars(self, *, validate: bool = True) -> pd.DataFrame:
        pair = self._to_binance(self.settings.symbol)
        interval = f"{self.settings.timeframe_minutes}m"
        # Binance caps a single klines call; request enough lookback bars.
        limit = min(self.settings.lookback_bars, 1000)
        raw = self._public("/api/v3/klines", {"symbol": pair, "interval": interval, "limit": limit})
        rows = [
            {
                "time": int(r[0]) / 1000.0,
                "open": float(r[1]), "high": float(r[2]), "low": float(r[3]),
                "close": float(r[4]), "volume": float(r[5]),
            }
            for r in raw
        ]
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("time").sort_index()
        if validate and hasattr(self, "check_freshness"):
            verdict = self.check_freshness(df)
            if not verdict.fresh:
                raise StaleDataError(verdict.reason)
        return df

    def get_ticker(self) -> dict:
        return self.get_ticker_for(self.settings.symbol)

    def get_ticker_for(self, symbol: str) -> dict:
        pair = self._to_binance(symbol)
        t = self._public("/api/v3/ticker/24hr", {"symbol": pair})
        return {
            "symbol": symbol,
            "last": float(t["lastPrice"]),
            "bid": float(t["bidPrice"]),
            "ask": float(t["askPrice"]),
            "volume": float(t.get("quoteVolume", 0)),
        }

    def check_freshness(self, bars: pd.DataFrame | None = None) -> FreshnessVerdict:
        if bars is None or len(bars) == 0:
            return FreshnessVerdict(fresh=False, reason="no bars",
                                    bar_age_minutes=1e9,
                                    max_age_minutes=self.settings.timeframe_minutes * 3)
        last = bars.index[-1]
        if hasattr(last, "tzinfo") and last.tzinfo is None:
            last = last.tz_localize("UTC")
        age = (pd.Timestamp.now("UTC") - last).total_seconds() / 60.0
        max_age = self.settings.timeframe_minutes * self.settings.max_bar_age_multiple
        return FreshnessVerdict(fresh=age <= max_age, reason=f"bar age {age:.1f}m",
                                bar_age_minutes=age, max_age_minutes=max_age)

    def market_quality(self) -> dict:
        t = self.get_ticker_for(self.settings.symbol)
        return {
            "bid": t["bid"], "ask": t["ask"],
            "recent_dollar_volume": t["volume"],
        }

    # ── account / positions ────────────────────────────────
    def account_equity(self) -> float:
        if not (self._key and self._secret):
            # No key → report the configured paper budget so sizing still works.
            return float(self.settings.strategy_equity_usd)
        bal = self._signed("/api/v3/account")
        usdt = 0.0
        for b in bal.get("balances", []):
            if b["asset"] in ("USDT", "USDC", "USD"):
                usdt += float(b["free"]) + float(b["locked"])
        # Add market value of held spot assets.
        prices = self._public("/api/v3/ticker/price")
        price_map = {p["symbol"]: float(p["price"]) for p in prices}
        for b in bal.get("balances", []):
            asset, free, locked = b["asset"], float(b["free"]), float(b["locked"])
            qty = free + locked
            if qty <= 0 or asset in ("USDT", "USDC", "USD"):
                continue
            pair = f"{asset}USDT"
            if pair in price_map:
                usdt += qty * price_map[pair]
        return round(usdt, 2)

    def positions(self) -> list[dict]:
        if not (self._key and self._secret):
            return []
        bal = self._signed("/api/v3/account")
        prices = self._public("/api/v3/ticker/price")
        price_map = {p["symbol"]: float(p["price"]) for p in prices}
        out = []
        for b in bal.get("balances", []):
            asset, free, locked = b["asset"], float(b["free"]), float(b["locked"])
            qty = free + locked
            if qty <= 0 or asset in ("USDT", "USDC", "USD"):
                continue
            pair = f"{asset}USDT"
            price = price_map.get(pair, 0.0)
            out.append({
                "symbol": self._from_binance(pair),
                "quantity": qty,
                "market_value": round(qty * price, 2),
                "average_entry": 0.0,
            })
        return out

    def has_position(self) -> bool:
        return len(self.positions()) > 0

    def orders(self) -> list[dict]:
        if not (self._key and self._secret):
            return []
        pair = self._to_binance(self.settings.symbol)
        o = self._signed("/api/v3/openOrders", {"symbol": pair})
        return [{
            "order_id": str(x["orderId"]),
            "symbol": self._from_binance(x["symbol"]),
            "side": x["side"].lower(),
            "status": x["status"].lower(),
        } for x in o]

    def find_order_by_userref(self, userref: int) -> dict | None:
        # Binance has no userref concept; mirror by our local idempotency key.
        # Best-effort: look among open orders for a matching client id tag.
        for o in self.orders():
            if str(userref) in str(o.get("order_id", "")):
                return o
        return None

    # ── sizing / execution ─────────────────────────────────
    def size_buy(self, notional_usd: float, price: float | None = None) -> _SizedOrder:
        meta = self.resolve_symbol()
        last = Decimal(str(price or self.get_ticker_for(self.settings.symbol)["last"]))
        if last <= 0:
            raise BrokerError("invalid price for sizing")
        raw_vol = Decimal(str(notional_usd)) / last
        step = meta.order_min or Decimal("1e-8")
        vol = (raw_vol // step) * step
        if vol <= Decimal("0"):
            raise BrokerError("notional too small for minimum lot")
        return _SizedOrder(
            pair=meta.key,
            volume=vol,
            price=last,
            notional=Decimal(str(notional_usd)),
        )

    def buy_notional(self, notional_usd: float, *, userref: int | None = None,
                     leverage: float | None = None) -> str:
        if not self.order_submission_enabled:
            self.audit.record(AuditEvent.ORDER_INTENT, {
                "broker": "binance", "side": "buy", "notional_usd": notional_usd,
                "userref": userref,
            })
            return f"binance-dry-buy-{userref}"
        # Validate the notional is sizeable on this pair (raises on below-min).
        self.size_buy(notional_usd)
        pair = self._to_binance(self.settings.symbol)
        params = {
            "symbol": pair, "side": "BUY", "type": "MARKET",
            "quoteOrderQty": f"{notional_usd:.2f}",
            "newClientOrderId": f"dublin-{userref}" if userref else None,
        }
        params = {k: v for k, v in params.items() if v is not None}
        try:
            resp = self._signed("/api/v3/order", params, method="POST")
            self.audit.record(AuditEvent.ORDER_SUBMITTED, {"broker": "binance", "order_id": resp.get("orderId"), "side": "buy"})
            return str(resp.get("orderId"))
        except BrokerError as exc:
            self.audit.record(AuditEvent.ORDER_REJECTED, {"broker": "binance", "error": str(exc)}, severity="error")
            raise

    def close_position(self, *, userref: int | None = None, quantity: float | None = None) -> str:
        pair = self._to_binance(self.settings.symbol)
        if not self.order_submission_enabled:
            self.audit.record(AuditEvent.ORDER_INTENT, {"broker": "binance", "side": "sell", "userref": userref})
            return f"binance-dry-sell-{userref}"
        params = {"symbol": pair, "side": "SELL", "type": "MARKET"}
        if quantity is not None:
            params["quantity"] = f"{quantity:.8f}"
        else:
            # Sell entire base balance.
            bal = self._signed("/api/v3/account")
            meta = self.resolve_symbol()
            qty = 0.0
            for b in bal.get("balances", []):
                if b["asset"] == meta.base:
                    qty = float(b["free"])
            if qty <= 0:
                return "binance-no-position"
            params["quantity"] = f"{qty:.8f}"
        params = {k: v for k, v in params.items() if v is not None}
        try:
            resp = self._signed("/api/v3/order", params, method="POST")
            self.audit.record(AuditEvent.ORDER_SUBMITTED, {"broker": "binance", "order_id": resp.get("orderId"), "side": "sell"})
            return str(resp.get("orderId"))
        except BrokerError as exc:
            self.audit.record(AuditEvent.ORDER_REJECTED, {"broker": "binance", "error": str(exc)}, severity="error")
            raise

    def add_order(self, params: dict) -> str:
        """Raw advanced order (limit / bracket). Mirrors KrakenGateway.add_order."""
        if not self.order_submission_enabled:
            self.audit.record(AuditEvent.ORDER_INTENT, {"broker": "binance", **params})
            return "binance-dry-" + str(params.get("type", "order"))
        payload = {"symbol": self._to_binance(self.settings.symbol)}
        payload.update(params)
        try:
            resp = self._signed("/api/v3/order", payload, method="POST")
            self.audit.record(AuditEvent.ORDER_SUBMITTED, {"broker": "binance", "order_id": resp.get("orderId")})
            return str(resp.get("orderId"))
        except BrokerError as exc:
            self.audit.record(AuditEvent.ORDER_REJECTED, {"broker": "binance", "error": str(exc)}, severity="error")
            raise

    def cancel_order(self, txid: str) -> bool:
        if not self.order_submission_enabled:
            return True
        pair = self._to_binance(self.settings.symbol)
        try:
            self._signed("/api/v3/order", {"symbol": pair, "orderId": txid}, method="DELETE")
            self.audit.record(AuditEvent.ORDER_CANCELLED, {"broker": "binance", "order_id": txid})
            return True
        except BrokerError:
            return False

    def cancel_attached(self, userref: int) -> int:
        """Cancel any open order tagged with our client id ``dublin-<userref>``.

        Binance has no bracket/OCA; attached SL/TP are independent orders keyed
        by ``newClientOrderId``.  We cancel by client-order-id mapping.
        """
        if not self.order_submission_enabled:
            return 0
        pair = self._to_binance(self.settings.symbol)
        client_id = f"dublin-{userref}"
        try:
            orders = self._signed("/api/v3/openOrders", {"symbol": pair})
            cancelled = 0
            for o in orders:
                if o.get("clientOrderId") == client_id:
                    self.cancel_order(str(o["orderId"]))
                    cancelled += 1
            return cancelled
        except BrokerError:
            return 0

    def margin_positions(self) -> list[dict]:
        """Spot adapter: no margin positions. Returns empty (feature-detected)."""
        return []

    # ── health ─────────────────────────────────────────────
    def health(self, *, include_freshness: bool = True) -> dict:
        try:
            t0 = time.time()
            self._public("/api/v3/ping")
            latency = (time.time() - t0) * 1000
            return {"broker": "binance", "reachable": True, "error": None,
                    "latency_ms": round(latency, 1)}
        except BrokerError as exc:
            return {"broker": "binance", "reachable": False, "error": str(exc)}

    @property
    def order_submission_enabled(self) -> bool:
        s = self.settings
        return self._allow_order_submission and s.allow_live_trading and not s.paper_trading and not s.dry_run

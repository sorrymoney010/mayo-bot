"""Binance Spot gateway (Binance.US by default) — a full peer of ``KrakenGateway``.

The engine, sleeves, risk manager, dashboard and audit log are broker-agnostic;
this module answers every gateway call they make with the same shapes the
Kraken adapter returns, so ``BROKER=binance`` is a drop-in switch.

Design notes
------------
* **Venue.** ``BINANCE_HOST`` defaults to ``binance.us`` (``api.binance.us``).
  ``binance.com`` or a full URL also work, but US residents must use Binance.US.
* **Symbols.** Our canonical ``"BTC/USD"`` resolves against live
  ``exchangeInfo``. Binance.US lists real USD pairs (``BTCUSD``); coins without
  one fall back through ``BINANCE_USD_QUOTES`` (default ``["USD", "USDT"]``).
  ``SymbolMeta.wsname`` stays canonical (``BTC/USD``) so the rest of the bot
  never sees exchange spelling.
* **Read-only by default.** Live submission needs ``dry_run=false``,
  ``paper_trading=false``, ``allow_live_trading=true``, the risk
  acknowledgement, *and* ``allow_order_submission=True`` on this object —
  exactly the Kraken rule. In paper/dry-run every signed endpoint is refused
  before a request is built (``PAPER_BLOCK_PRIVATE_API``).
* **Order ids** are ``"<SYMBOL>:<orderId>"`` (Binance ids are per symbol), so
  ``query_order`` / ``cancel_order`` never need extra context.
* **Idempotency.** ``userref`` travels as ``newClientOrderId="dublin-<userref>"``
  and is recovered from open *and* historical orders after a crash.
* **Brackets.** Binance has no Kraken ``close[...]`` block, so the protective
  stop is a separate ``STOP_LOSS`` sell placed right after the entry fills, keyed
  ``dublin-<userref+1>``; ``query_order(parent)["closetxid"]`` reports it.
* **Retries.** Reads retry transient errors with backoff; order mutations are
  never retried (an ambiguous submit is recovered via the userref lookup).

REST endpoints used (all ``/api/v3`` unless noted):
  GET  ping, time, exchangeInfo, klines, ticker/24hr, ticker/price   public
  GET  account, openOrders, allOrders, order, myTrades                signed
  GET  /sapi/v1/asset/query/trading-fee                               signed
  POST order, order/cancelReplace;  DELETE order                      signed (gated)
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
import urllib.parse
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from .audit import AuditEvent, AuditLog
from .config import Settings
from .errors import (
    AuthenticationError,
    BrokerError,
    InsufficientFundsError,
    InvalidRequestError,
    PaperPrivateCallBlocked,
    RateLimitError,
    SafetyLockError,
    TransientBrokerError,
)
from .execution_store import ExecutionFill, ExecutionStore, utc_now
from .freshness import FreshnessGuard, FreshnessVerdict, check_monotonic_bars
from .kraken_gateway import SymbolMeta
from .precision import SizedOrder, size_order

KNOWN_HOSTS = {
    "binance.us": "https://api.binance.us",
    "binanceus": "https://api.binance.us",
    "us": "https://api.binance.us",
    "binance.com": "https://api.binance.com",
    "com": "https://api.binance.com",
}
DEFAULT_HOST = KNOWN_HOSTS["binance.us"]

# Binance kline intervals, in minutes.
INTERVALS: dict[int, str] = {
    1: "1m", 3: "3m", 5: "5m", 15: "15m", 30: "30m",
    60: "1h", 120: "2h", 240: "4h", 360: "6h", 480: "8h", 720: "12h",
    1440: "1d", 4320: "3d", 10080: "1w",
}
STABLE_QUOTES = ("USD", "USDT", "USDC", "BUSD", "DAI", "FDUSD", "TUSD", "USDP")
CLIENT_PREFIX = "dublin-"
_BASE_ALIASES = {"XBT": "BTC", "XDG": "DOGE"}
_CLIENT_RE = re.compile(r"^dublin-(\d+)$")

_STATUS = {
    "NEW": "open", "PARTIALLY_FILLED": "open", "PENDING_NEW": "open",
    "FILLED": "closed",
    "CANCELED": "canceled", "PENDING_CANCEL": "canceled", "REJECTED": "canceled",
    "EXPIRED": "expired", "EXPIRED_IN_MATCH": "expired",
}
_ORDERTYPE_OUT = {
    "MARKET": "market", "LIMIT": "limit", "LIMIT_MAKER": "limit",
    "STOP_LOSS": "stop-loss", "STOP_LOSS_LIMIT": "stop-loss-limit",
    "TAKE_PROFIT": "take-profit", "TAKE_PROFIT_LIMIT": "take-profit-limit",
}
_READ_ONLY = {"/api/v3/account", "/api/v3/openOrders", "/api/v3/allOrders",
              "/api/v3/order", "/api/v3/myTrades", "/sapi/v1/asset/query/trading-fee"}


def host_for(settings: Settings) -> str:
    raw = str(getattr(settings, "binance_host", "") or "").strip()
    if not raw:
        return DEFAULT_HOST
    if raw.lower() in KNOWN_HOSTS:
        return KNOWN_HOSTS[raw.lower()]
    if not raw.startswith("http"):
        raw = "https://" + raw
    return raw.rstrip("/")


def interval_for(minutes: int) -> tuple[int, str]:
    """Nearest Binance kline interval to ``minutes`` -> (minutes, code)."""
    mins = int(minutes)
    if mins in INTERVALS:
        return mins, INTERVALS[mins]
    best = min(INTERVALS, key=lambda v: abs(v - mins))
    return best, INTERVALS[best]


def _dec(value: object, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else default))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def _decimals(step: Decimal) -> int:
    if step <= 0:
        return 8
    exp = step.normalize().as_tuple().exponent
    return max(0, -int(exp))


def _floor_to(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _plain(value: Decimal) -> str:
    s = format(value.normalize(), "f")
    return s if s not in ("", "-0") else "0"


def client_id_for(userref: int | str | None) -> str | None:
    return f"{CLIENT_PREFIX}{int(userref)}" if userref not in (None, "") else None


def userref_from_client_id(client_id: object) -> int | None:
    m = _CLIENT_RE.match(str(client_id or ""))
    return int(m.group(1)) if m else None


def classify_binance_error(status: int, code: object, msg: str) -> BrokerError:
    """Map a Binance error response onto the shared exception hierarchy."""
    try:
        c = int(code)
    except (TypeError, ValueError):
        c = 0
    text = f"Binance error {c}: {msg}" if c else f"HTTP {status} from Binance: {msg}"
    low = (msg or "").lower()
    if status in (418, 429) or c in (-1003, -1015):
        return RateLimitError(text)
    if c in (-1002, -1022, -2014, -2015) or status in (401,):
        return AuthenticationError(text)
    if c == -1021:
        return TransientBrokerError(text)  # timestamp drift: resync and retry
    if c in (-1000, -1001, -1006, -1007, -1008) or status >= 500:
        return TransientBrokerError(text)
    if "insufficient" in low and "balance" in low:
        return InsufficientFundsError(text)
    if status == 403:
        # WAF / geo-block (e.g. binance.com from the US): permanent here.
        return AuthenticationError(text)
    return InvalidRequestError(text)


class BinanceGateway:
    """Binance Spot adapter implementing the broker-agnostic gateway contract."""

    def __init__(
        self,
        settings: Settings,
        *,
        audit: AuditLog | None = None,
        session: Any | None = None,
        allow_order_submission: bool = False,
        sleep_fn: Any | None = None,
        max_retries: int | None = None,
        time_fn: Any | None = None,
    ) -> None:
        self.settings = settings
        self._audit = audit
        self._session = session or requests.Session()
        try:
            self._session.headers.update({"User-Agent": "DublinBot/0.2"})
        except AttributeError:
            pass
        self._host = host_for(settings)
        self._api_key = settings.binance_api_key or ""
        self._api_secret = settings.binance_api_secret or ""
        self._allow_order_submission = bool(allow_order_submission)
        self._sleep = sleep_fn or time.sleep
        self._time = time_fn or time.time
        self._max_retries = int(max_retries or getattr(settings, "max_retries", 3) or 3)
        self._timeout = settings.http_timeout_seconds
        self._recv_window = int(getattr(settings, "binance_recv_window_ms", 5000) or 5000)
        self._time_offset_ms: int | None = None
        self._used_weight: int | None = None
        self._executions = ExecutionStore(settings.execution_db_path)
        self.last_fill: ExecutionFill | None = None
        self._closetxid: dict[str, str] = {}

        self._info: dict[str, dict] = {}
        self._info_loaded_at = 0.0
        self._info_ttl = 3600.0
        self.freshness = FreshnessGuard(
            interval_for(settings.timeframe_minutes)[0],
            max_bar_age_multiple=settings.max_bar_age_multiple,
            max_clock_skew_seconds=settings.max_clock_skew_seconds,
        )
        self.last_freshness: FreshnessVerdict | None = None

    # ── introspection ──────────────────────────────────────
    @property
    def name(self) -> str:
        return "binance"

    @property
    def host(self) -> str:
        return self._host

    @property
    def has_credentials(self) -> bool:
        return bool(self._api_key and self._api_secret)

    @property
    def order_submission_enabled(self) -> bool:
        """True only when every independent safety gate permits live orders."""
        s = self.settings
        return (
            self._allow_order_submission
            and not s.dry_run
            and not s.paper_trading
            and s.allow_live_trading
            and s.live_risk_acknowledgement == "I_ACCEPT_LIVE_TRADING_RISK"
        )

    # Back-compat alias used by older tests/callers.
    @property
    def audit(self) -> AuditLog:
        if self._audit is None:
            self._audit = AuditLog(Path(self.settings.audit_log_path))
        return self._audit

    def _log(self, event: AuditEvent, payload: dict, severity: str = "info") -> None:
        if self._audit is not None:
            self._audit.record(event, {"broker": "binance", **payload}, severity=severity)

    # ── transport ──────────────────────────────────────────
    def _request(self, method: str, url: str, *, params: dict | None = None,
                 body: str | None = None, headers: dict | None = None) -> Any:
        try:
            if method == "GET":
                resp = self._session.get(url, params=params, headers=headers, timeout=self._timeout)
            elif method == "POST":
                resp = self._session.post(url, data=body, headers=headers, timeout=self._timeout)
            elif method == "DELETE":
                resp = self._session.delete(url, params=params, headers=headers, timeout=self._timeout)
            else:  # pragma: no cover - programming error
                raise ValueError(method)
        except requests.Timeout as exc:
            raise TransientBrokerError(f"timeout calling {url}") from exc
        except requests.RequestException as exc:
            raise TransientBrokerError(f"network error calling {url}: {exc}") from exc

        status = int(getattr(resp, "status_code", 200) or 200)
        try:
            weight = (getattr(resp, "headers", None) or {}).get("X-MBX-USED-WEIGHT-1M")
            if weight is not None:
                self._used_weight = int(weight)
        except (TypeError, ValueError):
            pass
        try:
            payload = resp.json()
        except ValueError as exc:
            if status >= 400:
                raise classify_binance_error(status, None, getattr(resp, "text", "")[:200]) from exc
            raise BrokerError("Binance returned a non-JSON response") from exc
        if isinstance(payload, dict) and isinstance(payload.get("code"), int) and payload["code"] < 0:
            raise classify_binance_error(status, payload.get("code"), str(payload.get("msg", "")))
        if status >= 400:
            msg = payload.get("msg", "") if isinstance(payload, dict) else ""
            raise classify_binance_error(status, payload.get("code") if isinstance(payload, dict) else None, str(msg))
        return payload

    def _with_retries(self, description: str, call):
        delay = 1.0
        last: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                return call()
            except (RateLimitError, TransientBrokerError) as exc:
                last = exc
                if isinstance(exc, RateLimitError):
                    self._log(AuditEvent.RATE_LIMIT, {"operation": description, "attempt": attempt},
                              severity="warning")
                if "-1021" in str(exc):
                    self._time_offset_ms = None  # force a clock resync
                if attempt == self._max_retries:
                    break
                self._sleep(delay)
                delay *= 2
        assert last is not None
        raise last

    def _public(self, path: str, params: dict | None = None) -> Any:
        if not path.startswith("/"):
            # Kraken endpoint name ("OHLC", "Ticker", "Time"): research scripts
            # and the dashboard call these; answer in Kraken's result shape.
            return self._kraken_public(path, dict(params or {}))
        url = f"{self._host}{path}"
        return self._with_retries(f"public:{path}",
                                  lambda: self._request("GET", url, params=params or {}))

    def _assert_private_allowed(self, path: str) -> None:
        st = self.settings
        if (getattr(st, "paper_block_private_api", True)
                and (getattr(st, "paper_trading", False) or getattr(st, "dry_run", False))):
            raise PaperPrivateCallBlocked(
                f"private Binance endpoint '{path}' blocked: paper/dry-run mode never "
                "calls private endpoints (PAPER_BLOCK_PRIVATE_API)")
        if not self.has_credentials:
            raise AuthenticationError("Binance credentials required for private endpoints")

    def _timestamp_ms(self) -> int:
        if self._time_offset_ms is None:
            try:
                server = int(self._public("/api/v3/time")["serverTime"])
                self._time_offset_ms = server - int(self._time() * 1000)
            except (BrokerError, KeyError, TypeError, ValueError):
                self._time_offset_ms = 0
        return int(self._time() * 1000) + int(self._time_offset_ms or 0)

    def _sign(self, query: str) -> str:
        return hmac.new(self._api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()

    def _signed(self, path: str, params: dict | None = None, *, method: str = "GET") -> Any:
        self._assert_private_allowed(path)
        url = f"{self._host}{path}"
        headers = {"X-MBX-APIKEY": self._api_key}

        def call() -> Any:
            body = {k: v for k, v in dict(params or {}).items() if v is not None}
            body["recvWindow"] = self._recv_window
            body["timestamp"] = self._timestamp_ms()
            query = urllib.parse.urlencode(body)
            signed = f"{query}&signature={self._sign(query)}"
            if method == "POST":
                hdrs = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
                return self._request("POST", url, body=signed, headers=hdrs)
            return self._request(method, f"{url}?{signed}", headers=headers)

        if method == "GET" and path in _READ_ONLY:
            return self._with_retries(f"private:{path}", call)
        try:
            return call()  # mutations are never blindly retried
        except TransientBrokerError as exc:
            if "-1021" in str(exc):
                # Rejected before matching (timestamp outside recvWindow) — the
                # order did not land, so one resynced retry is safe.
                self._time_offset_ms = None
                return call()
            raise

    # ── symbol metadata ────────────────────────────────────
    def load_metadata(self, force: bool = False) -> dict[str, dict]:
        if self._info and not force and (time.time() - self._info_loaded_at) < self._info_ttl:
            return self._info
        data = self._public("/api/v3/exchangeInfo")
        info = {s["symbol"]: s for s in (data.get("symbols") or []) if isinstance(s, dict)}
        if not info:
            raise BrokerError("Binance exchangeInfo returned no symbols")
        self._info = info
        self._info_loaded_at = time.time()
        return info

    def _quote_prefs(self, quote: str) -> list[str]:
        quote = quote.upper()
        if quote in ("USD", "ZUSD"):
            prefs = list(getattr(self.settings, "binance_usd_quotes", None) or ["USD", "USDT"])
            return [q.upper() for q in prefs]
        return [quote]

    def _pair_for(self, symbol: str) -> str:
        raw = str(symbol or "").strip().upper()
        if ":" in raw:
            raw = raw.split(":", 1)[0]
        info = self.load_metadata()
        if raw in info:
            return raw
        compact = raw.replace("-", "/").replace("_", "/")
        if "/" in compact:
            base, _, quote = compact.partition("/")
        else:
            # "XBTUSD" also ends in "TUSD": accept only a split whose base is
            # a real listed asset.
            known = {str(v.get("baseAsset", "")).upper() for v in info.values()}
            base, quote = compact, ""
            for q in sorted(STABLE_QUOTES, key=len, reverse=True):
                if compact.endswith(q) and len(compact) > len(q):
                    cand = compact[: -len(q)]
                    if _BASE_ALIASES.get(cand, cand) in known:
                        base, quote = cand, q
                        break
        base = _BASE_ALIASES.get(base, base)
        tried = []
        for q in self._quote_prefs(quote or "USD"):
            pair = f"{base}{q}"
            tried.append(pair)
            s = info.get(pair)
            if s and str(s.get("status", "TRADING")).upper() == "TRADING":
                return pair
        for pair in tried:
            if pair in info:
                return pair
        raise InvalidRequestError(
            f"Binance ({self._host}) has no pair matching {symbol!r} (tried: {', '.join(tried)})")

    def _filters(self, pair: str) -> dict[str, dict]:
        s = self.load_metadata().get(pair) or {}
        return {f.get("filterType"): f for f in s.get("filters", []) if isinstance(f, dict)}

    def _steps(self, pair: str) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        """(step_size, min_qty, tick_size, min_notional) for a pair."""
        f = self._filters(pair)
        lot = f.get("LOT_SIZE", {})
        price = f.get("PRICE_FILTER", {})
        notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
        return (_dec(lot.get("stepSize"), "0.00000001"), _dec(lot.get("minQty")),
                _dec(price.get("tickSize"), "0.00000001"), _dec(notional.get("minNotional")))

    def _canonical(self, pair: str) -> str:
        s = self.load_metadata().get(pair) or {}
        base = str(s.get("baseAsset") or pair)
        quote = str(s.get("quoteAsset") or "")
        return f"{base}/USD" if quote in STABLE_QUOTES else f"{base}/{quote}"

    def _meta(self, pair: str) -> SymbolMeta:
        s = self.load_metadata()[pair]
        step, min_qty, tick, min_notional = self._steps(pair)
        status = str(s.get("status", "TRADING")).upper()
        return SymbolMeta(
            key=pair,
            altname=pair,
            wsname=self._canonical(pair),
            base=str(s.get("baseAsset", "")),
            quote=str(s.get("quoteAsset", "")),
            lot_decimals=_decimals(step),
            pair_decimals=_decimals(tick),
            order_min=max(min_qty, step if step > 0 else Decimal("0")),
            cost_min=min_notional,
            status="online" if status == "TRADING" else status.lower(),
        )

    def resolve_symbol(self, symbol: str | None = None) -> SymbolMeta:
        """Resolve ``BTC/USD`` (or ``BTCUSD`` / ``BTCUSD:123``) to live metadata."""
        return self._meta(self._pair_for(symbol or self.settings.symbol))

    @property
    def pair(self) -> str:
        return self.resolve_symbol().key

    def asset_info(self, symbol: str | None = None) -> SymbolMeta | None:
        try:
            return self.resolve_symbol(symbol)
        except InvalidRequestError:
            return None

    def list_usd_pairs(self) -> list[SymbolMeta]:
        """Every TRADING */USD-like pair, one per base (quote preference order)."""
        info = self.load_metadata()
        prefs = self._quote_prefs("USD")
        chosen: dict[str, str] = {}
        for q in reversed(prefs):  # later prefs first, earlier ones overwrite
            for pair, s in info.items():
                if s.get("quoteAsset") == q and str(s.get("status", "")).upper() == "TRADING":
                    chosen[str(s.get("baseAsset"))] = pair
        return [self._meta(p) for p in sorted(chosen.values())]

    # ── market data ────────────────────────────────────────
    def kraken_interval(self) -> int:
        """Name kept for engine compatibility: the effective bar size in minutes."""
        return interval_for(self.settings.timeframe_minutes)[0]

    def server_time(self) -> float:
        return float(self._public("/api/v3/time")["serverTime"]) / 1000.0

    def pipeline_source(self):
        # The tick pipeline records Kraken trades; never mix venues' prices.
        return None

    def klines(self, symbol: str, tf_minutes: int, *, limit: int = 500,
               end_ms: int | None = None) -> list[list]:
        pair = self._pair_for(symbol)
        _, code = interval_for(tf_minutes)
        params: dict[str, Any] = {"symbol": pair, "interval": code, "limit": max(1, min(int(limit), 1000))}
        if end_ms is not None:
            params["endTime"] = int(end_ms)
        rows = self._public("/api/v3/klines", params)
        if not isinstance(rows, list):
            raise BrokerError(f"unexpected klines payload for {pair}")
        return rows

    def get_rest_bars(self, *, validate: bool = True) -> pd.DataFrame:
        want = int(self.settings.lookback_bars) + 1
        rows: list[list] = []
        end_ms: int | None = None
        while len(rows) < want:
            page = self.klines(self.settings.symbol, self.settings.timeframe_minutes,
                               limit=min(1000, want - len(rows)), end_ms=end_ms)
            if not page:
                break
            rows = page + rows
            end_ms = int(page[0][0]) - 1
            if len(page) < 2:
                break
        if not rows:
            raise BrokerError(f"No kline data returned for {self.settings.symbol}")
        now_ms = int(self._time() * 1000)
        dedup: dict[int, list] = {}
        for r in rows:
            if int(r[6]) < now_ms:  # closeTime in the past => candle is closed
                dedup[int(r[0])] = r
        ordered = [dedup[k] for k in sorted(dedup)]
        frame = pd.DataFrame(
            [{
                "open": float(r[1]), "high": float(r[2]), "low": float(r[3]),
                "close": float(r[4]),
                "vwap": (float(r[7]) / float(r[5])) if float(r[5]) > 0 else float(r[4]),
                "volume": float(r[5]), "trades": int(r[8]),
            } for r in ordered],
            index=pd.DatetimeIndex([pd.Timestamp(int(r[0]), unit="ms", tz="UTC") for r in ordered],
                                   name="timestamp"),
        ).sort_index()
        if validate:
            check_monotonic_bars(frame)
        return frame.tail(self.settings.lookback_bars)

    def get_bars(self, *, validate: bool = True) -> pd.DataFrame:
        return self.get_rest_bars(validate=validate)

    def get_bars_for(self, symbol: str) -> pd.DataFrame:
        from copy import copy
        view = copy(self)
        view.settings = copy(self.settings)
        view.settings.symbol = symbol
        return view.get_rest_bars(validate=True)

    def get_ticker(self) -> dict:
        return self.get_ticker_for(self.settings.symbol)

    def get_ticker_for(self, symbol: str) -> dict:
        pair = self._pair_for(symbol)
        t = self._public("/api/v3/ticker/24hr", {"symbol": pair})
        if not isinstance(t, dict) or "lastPrice" not in t:
            raise BrokerError(f"No ticker data for {pair}")
        return {
            "symbol": symbol,
            "pair": pair,
            "bid": float(t.get("bidPrice") or 0),
            "ask": float(t.get("askPrice") or 0),
            "last": float(t["lastPrice"]),
            "volume_24h": float(t.get("volume") or 0),
            "vwap_24h": float(t.get("weightedAvgPrice") or 0),
            "trades_24h": int(t.get("count") or 0),
            "quote_volume_24h": float(t.get("quoteVolume") or 0),
            "open_24h": float(t.get("openPrice") or 0),
            "high_24h": float(t.get("highPrice") or 0),
            "low_24h": float(t.get("lowPrice") or 0),
            "change_24h_pct": float(t.get("priceChangePercent") or 0),
        }

    def last_prices(self) -> dict[str, float]:
        rows = self._public("/api/v3/ticker/price")
        return {str(r["symbol"]): float(r["price"]) for r in rows if isinstance(r, dict)}

    def check_freshness(self, bars: pd.DataFrame | None = None) -> FreshnessVerdict:
        try:
            server = self.server_time()
        except BrokerError:
            server = None
        frame = self.get_bars() if bars is None else bars
        verdict = self.freshness.evaluate_bars(frame, server_time=server)
        self.last_freshness = verdict
        self._log(AuditEvent.DATA_FRESHNESS, verdict.to_dict(),
                  severity="info" if verdict.fresh else "warning")
        return verdict

    def market_quality(self) -> dict:
        t = self.get_ticker()
        bid, ask = t["bid"], t["ask"]
        mid = (bid + ask) / 2
        return {
            "bid": bid, "ask": ask, "mid": mid,
            "spread_bps": ((ask - bid) / mid) * 10_000 if mid > 0 else float("inf"),
            "recent_dollar_volume": t["quote_volume_24h"] or t["volume_24h"] * t["vwap_24h"],
        }

    # ── account ────────────────────────────────────────────
    def _account(self) -> dict:
        return self._signed("/api/v3/account", {"omitZeroBalances": "true"})

    def balances(self) -> dict[str, float]:
        if not self.has_credentials:
            return {}
        out: dict[str, float] = {}
        for b in self._account().get("balances", []):
            qty = float(b.get("free") or 0) + float(b.get("locked") or 0)
            if qty:
                out[str(b["asset"])] = qty
        return out

    def free_balance(self, asset: str) -> float:
        for b in self._account().get("balances", []):
            if b.get("asset") == asset:
                return float(b.get("free") or 0)
        return 0.0

    def account_equity(self) -> float:
        """Stablecoin/USD cash plus every holding marked at its USD-like price."""
        if not self.has_credentials:
            return float(self.settings.strategy_equity_usd)
        try:
            balances = self.balances()
            prices = self.last_prices()
        except BrokerError as exc:
            self._log(AuditEvent.BROKER_ERROR, {"operation": "account_equity", "error": str(exc)},
                      severity="warning")
            return float(self.settings.strategy_equity_usd)
        total = 0.0
        for asset, qty in balances.items():
            if asset in STABLE_QUOTES:
                total += qty
                continue
            for q in ("USD", "USDT", "USDC"):
                px = prices.get(f"{asset}{q}")
                if px:
                    total += qty * px
                    break
        return round(total, 2)

    def positions(self) -> list[dict]:
        """Spot holding of the configured symbol's base asset (Kraken shape)."""
        if not self.has_credentials:
            return []
        try:
            meta = self.resolve_symbol()
            balances = self.balances()
        except BrokerError as exc:
            self._log(AuditEvent.BROKER_ERROR, {"operation": "positions", "error": str(exc)},
                      severity="warning")
            return []
        qty = float(balances.get(meta.base, 0.0))
        if qty <= float(meta.order_min or 0):
            return []
        try:
            price = self.get_ticker()["last"]
        except BrokerError:
            price = 0.0
        return [{
            "symbol": meta.altname, "asset": meta.base, "side": "long",
            "quantity": qty, "average_entry": 0.0,
            "market_value": qty * price, "unrealized_pl": 0.0,
        }]

    def has_position(self) -> bool:
        return bool(self.positions())

    def margin_positions(self) -> list[dict]:
        """Spot-only venue (Binance.US offers no margin): always empty."""
        return []

    def fee_schedule(self, pair: str | None = None) -> dict[str, float] | None:
        """Live maker/taker fee in BPS for ``pair`` (or the configured symbol)."""
        if not self.has_credentials:
            return None
        try:
            pair = self._pair_for(pair or self.settings.symbol)
        except BrokerError:
            return None
        try:
            rows = self._signed("/sapi/v1/asset/query/trading-fee", {"symbol": pair})
            row = rows[0] if isinstance(rows, list) and rows else rows
            maker = float(row["makerCommission"]) * 10_000
            taker = float(row["takerCommission"]) * 10_000
            return {"taker_bps": taker, "maker_bps": maker}
        except (BrokerError, KeyError, TypeError, ValueError, IndexError):
            pass
        try:
            acct = self._account()
            rates = acct.get("commissionRates") or {}
            if rates:
                return {"taker_bps": float(rates["taker"]) * 10_000,
                        "maker_bps": float(rates["maker"]) * 10_000}
            # Legacy integer fields are in basis points.
            return {"taker_bps": float(acct["takerCommission"]),
                    "maker_bps": float(acct["makerCommission"])}
        except (BrokerError, KeyError, TypeError, ValueError):
            return None

    def _trade_symbols(self) -> list[str]:
        syms = [self.settings.symbol, *list(getattr(self.settings, "coin_basket", []) or [])]
        for name in ("meanrev_symbols", "trendhold_symbols", "breakout_symbols"):
            syms.extend(list(getattr(self.settings, name, []) or []))
        pairs: list[str] = []
        for sym in syms:
            try:
                pair = self._pair_for(sym)
            except BrokerError:
                continue
            if pair not in pairs:
                pairs.append(pair)
        return pairs

    def my_trades(self, pair: str, *, max_pages: int = 20) -> list[dict]:
        rows: list[dict] = []
        from_id = 0
        for _ in range(max_pages):
            page = self._signed("/api/v3/myTrades", {"symbol": pair, "fromId": from_id, "limit": 1000})
            if not page:
                break
            rows.extend(page)
            if len(page) < 1000:
                break
            from_id = int(page[-1]["id"]) + 1
        return rows

    def closed_trade_pnl(self, since: int = 0) -> tuple[list[dict], int]:
        """Realized P&L per SELL fill (average-cost basis), unix-SECONDS cursor.

        Binance reports no per-trade P&L, so cost basis is rebuilt from the
        account's full fill history per traded pair. Rows carry a stable
        ``txid`` (``PAIR-tradeId``) so the learner de-duplicates re-fetches.
        """
        out: list[dict] = []
        latest = int(since)
        for pair in self._trade_symbols():
            trades = sorted(self.my_trades(pair), key=lambda t: (int(t.get("time", 0)), int(t.get("id", 0))))
            meta = self.load_metadata().get(pair, {})
            base, quote = meta.get("baseAsset", ""), meta.get("quoteAsset", "")
            qty_held = 0.0
            cost = 0.0
            for t in trades:
                qty = float(t.get("qty") or 0)
                quote_qty = float(t.get("quoteQty") or 0) or qty * float(t.get("price") or 0)
                price = float(t.get("price") or 0)
                fee_q = _fee_in_quote(t, base, quote, price)
                ts = int(int(t.get("time", 0)) / 1000)
                if t.get("isBuyer"):
                    if t.get("commissionAsset") == base:
                        qty -= float(t.get("commission") or 0)
                    qty_held += max(qty, 0.0)
                    cost += quote_qty + (fee_q if t.get("commissionAsset") != base else 0.0)
                    continue
                if qty_held <= 0:
                    continue
                sold = min(qty, qty_held)
                avg = cost / qty_held if qty_held else 0.0
                pnl = sold * price - sold * avg - fee_q
                cost -= sold * avg
                qty_held -= sold
                if ts >= int(since):
                    latest = max(latest, ts)
                    out.append({"txid": f"{pair}-{t.get('id')}", "symbol": pair,
                                "pnl": round(pnl, 8), "ts": ts})
        return out, latest

    # ── orders (read) ──────────────────────────────────────
    @staticmethod
    def order_id(pair: str, order_id: object) -> str:
        return f"{pair}:{order_id}"

    def _split_id(self, txid: str) -> tuple[str, str]:
        raw = str(txid)
        if ":" in raw:
            pair, _, oid = raw.partition(":")
            return pair.upper(), oid
        return self._pair_for(self.settings.symbol), raw

    def _shape(self, o: dict, fills: list[dict] | None = None) -> dict:
        """Binance order -> the Kraken QueryOrders shape the engine reads."""
        pair = str(o.get("symbol", ""))
        meta = self.load_metadata().get(pair, {})
        base, quote = meta.get("baseAsset", ""), meta.get("quoteAsset", "")
        vol_exec = float(o.get("executedQty") or 0)
        cost = float(o.get("cummulativeQuoteQty") or 0)
        if cost < 0:
            cost = 0.0
        price = cost / vol_exec if vol_exec > 0 and cost > 0 else float(o.get("price") or 0)
        fee = 0.0
        for f in fills or []:
            fee += _fee_in_quote(f, base, quote, float(f.get("price") or price or 0))
        status = _STATUS.get(str(o.get("status", "")).upper(), "unknown")
        txid = self.order_id(pair, o.get("orderId"))
        shaped = {
            "id": txid,
            "status": status,
            "vol": float(o.get("origQty") or 0),
            "vol_exec": vol_exec,
            "cost": cost,
            "price": price,
            "fee": fee,
            "stopprice": float(o.get("stopPrice") or 0),
            "opentm": float(o.get("time") or o.get("transactTime") or 0) / 1000.0,
            "userref": userref_from_client_id(o.get("clientOrderId")),
            "client_order_id": o.get("clientOrderId"),
            "descr": {
                "pair": pair,
                "type": str(o.get("side", "")).lower(),
                "ordertype": _ORDERTYPE_OUT.get(str(o.get("type", "")).upper(),
                                                str(o.get("type", "")).lower()),
                "price": str(o.get("price", "")),
            },
        }
        if txid in self._closetxid:
            shaped["closetxid"] = self._closetxid[txid]
        return shaped

    def _flat(self, shaped: dict) -> dict:
        return {
            "id": shaped["id"],
            "symbol": shaped["descr"]["pair"],
            "side": shaped["descr"]["type"],
            "order_type": shaped["descr"]["ordertype"],
            "status": shaped["status"],
            "volume": shaped["vol"],
            "volume_executed": shaped["vol_exec"],
            "notional": shaped["cost"],
            "userref": shaped["userref"],
        }

    def open_orders_raw(self) -> list[dict]:
        rows = self._signed("/api/v3/openOrders")
        return rows if isinstance(rows, list) else []

    def orders(self) -> list[dict]:
        if not self.has_credentials:
            return []
        try:
            return [self._flat(self._shape(o)) for o in self.open_orders_raw()]
        except BrokerError as exc:
            self._log(AuditEvent.BROKER_ERROR, {"operation": "orders", "error": str(exc)},
                      severity="warning")
            return []

    def find_order_by_userref(self, userref: int) -> dict | None:
        """Did an order with this idempotency key land? Open book, then history."""
        if not self.has_credentials:
            return None
        for order in self.orders():
            if order.get("userref") == int(userref):
                return order
        cid = client_id_for(userref)
        for pair in self._trade_symbols():
            try:
                o = self._signed("/api/v3/order", {"symbol": pair, "origClientOrderId": cid})
            except InvalidRequestError:
                continue  # -2013 order does not exist on this pair
            except BrokerError:
                return None
            if isinstance(o, dict) and o.get("orderId") is not None:
                return self._flat(self._shape(o))
        return None

    def find_children_by_userref(self, userref: int) -> list[dict]:
        if not self.has_credentials:
            return []
        return [o for o in self.orders() if o.get("userref") == int(userref)]

    def query_order(self, txid: str) -> dict:
        pair, oid = self._split_id(txid)
        o = self._signed("/api/v3/order", {"symbol": pair, "orderId": oid})
        if not isinstance(o, dict) or o.get("orderId") is None:
            raise BrokerError(f"Binance did not return order {txid}")
        fills: list[dict] = []
        if float(o.get("executedQty") or 0) > 0:
            try:
                fills = self._signed("/api/v3/myTrades", {"symbol": pair, "orderId": oid}) or []
            except BrokerError:
                fills = []
        return self._shape(o, fills)

    # ── sizing ─────────────────────────────────────────────
    def _qty_str(self, pair: str, qty: float | Decimal) -> str:
        step, _, _, _ = self._steps(pair)
        return _plain(_floor_to(_dec(qty), step))

    def _price_str(self, pair: str, price: float | Decimal) -> str:
        _, _, tick, _ = self._steps(pair)
        return _plain(_floor_to(_dec(price), tick))

    def size_buy(self, notional_usd: float, price: float | None = None) -> SizedOrder:
        """Validate a USD notional against live LOT_SIZE / PRICE / NOTIONAL filters."""
        meta = self.resolve_symbol()
        if price is None:
            price = self.get_ticker()["ask"]
        sized = size_order(notional_usd, price, meta.to_precision(),
                           min_notional_usd=self.settings.min_order_notional_usd)
        step, _, tick, _ = self._steps(meta.key)
        volume = _floor_to(sized.volume, step)
        if volume != sized.volume:  # non power-of-ten step size
            from .errors import PrecisionError
            if volume <= 0 or volume < meta.order_min:
                raise PrecisionError(f"volume {volume} below Binance minimum for {meta.key}")
            sized = SizedOrder(pair=sized.pair, volume=volume, price=_floor_to(sized.price, tick),
                               notional=volume * _dec(price))
        return sized

    # ── execution (gated) ──────────────────────────────────
    def _assert_can_submit(self) -> None:
        if not self.order_submission_enabled:
            raise SafetyLockError(
                "Live order submission is disabled. Required: dry_run=false, "
                "paper_trading=false, allow_live_trading=true, a valid "
                "acknowledgement, and allow_order_submission=true on the gateway.")

    def _submit(self, params: dict) -> dict:
        self._assert_can_submit()
        body = {k: v for k, v in params.items() if v is not None}
        body.setdefault("newOrderRespType", "FULL")
        return self._signed("/api/v3/order", body, method="POST")

    def _record_fill(self, resp: dict, *, side: str, signal_price: float) -> ExecutionFill | None:
        shaped = self._shape(resp, resp.get("fills") or [])
        if shaped["vol_exec"] <= 0 or shaped["price"] <= 0:
            return None
        expected = float(signal_price or 0)
        price = shaped["price"]
        signed = price - expected if side == "buy" else expected - price
        fill = ExecutionFill(
            order_id=shaped["id"], symbol=shaped["descr"]["pair"], side=side,
            status=shaped["status"], signal_price=expected, fill_price=price,
            slippage_usd=signed, slippage_bps=(signed / expected * 10_000) if expected > 0 else 0.0,
            volume=shaped["vol_exec"], cost=shaped["cost"], fee=shaped["fee"], filled_at=utc_now(),
        )
        self._executions.save(fill)
        self.last_fill = fill
        self._log(AuditEvent.ORDER_FILLED, fill.to_dict())
        return fill

    def _capture_fill(self, txid: str, *, side: str, signal_price: float,
                      resp: dict | None = None, attempts: int = 5) -> ExecutionFill | None:
        if resp and str(resp.get("status", "")).upper() == "FILLED":
            return self._record_fill(resp, side=side, signal_price=signal_price)
        for attempt in range(max(1, attempts)):
            try:
                pair, oid = self._split_id(txid)
                o = self._signed("/api/v3/order", {"symbol": pair, "orderId": oid})
                if str(o.get("status", "")).upper() == "FILLED" or float(o.get("executedQty") or 0) > 0:
                    fills = self._signed("/api/v3/myTrades", {"symbol": pair, "orderId": oid}) or []
                    if str(o.get("status", "")).upper() == "FILLED":
                        return self._record_fill({**o, "fills": fills}, side=side, signal_price=signal_price)
                if _STATUS.get(str(o.get("status", "")).upper()) in ("canceled", "expired"):
                    return None
            except BrokerError as exc:
                if attempt + 1 == attempts:
                    self._log(AuditEvent.BROKER_ERROR, {"operation": "QueryOrder", "order_id": txid,
                                                        "error": str(exc)}, severity="warning")
                    return None
            self._sleep(0.25)
        self._log(AuditEvent.BROKER_ERROR, {"operation": "QueryOrder", "order_id": txid,
                                            "error": "fill not confirmed before polling deadline"},
                  severity="warning")
        return None

    def buy_notional(self, notional_usd: float, *, userref: int | None = None,
                     signal_price: float | None = None, leverage: float | None = None) -> str:
        sized = self.size_buy(notional_usd)
        if not self.order_submission_enabled:
            self._log(AuditEvent.ORDER_INTENT, {
                "mode": "dry_run", "pair": sized.pair, "side": "buy",
                "volume": sized.volume_str, "price": sized.price_str,
                "notional": str(sized.notional), "userref": userref,
            })
            return f"binance-dry-buy-{userref or int(time.time())}"
        resp = self._submit({"symbol": sized.pair, "side": "BUY", "type": "MARKET",
                             "quantity": self._qty_str(sized.pair, sized.volume),
                             "newClientOrderId": client_id_for(userref)})
        txid = self.order_id(sized.pair, resp.get("orderId"))
        self._log(AuditEvent.ORDER_SUBMITTED, {"pair": sized.pair, "side": "buy",
                                               "volume": sized.volume_str, "order_id": txid,
                                               "userref": userref}, severity="warning")
        self._capture_fill(txid, side="buy", signal_price=float(signal_price or sized.price), resp=resp)
        return txid

    def close_position(self, *, userref: int | None = None, quantity: float | None = None,
                       signal_price: float | None = None) -> str:
        """Sell at most ``quantity`` of the configured base (never a full sweep)."""
        if quantity is None or quantity <= 0:
            raise BrokerError("Refusing to close position with quantity=None (would sweep full balance)")
        meta = self.resolve_symbol()
        if not self.order_submission_enabled:
            self._log(AuditEvent.ORDER_INTENT, {"mode": "dry_run", "pair": meta.key, "side": "sell",
                                                "volume": format(float(quantity), "f"),
                                                "userref": userref})
            return f"binance-dry-close-{userref or int(time.time())}"
        free = self.free_balance(meta.base)
        if free <= 0:
            raise BrokerError("No position to close")
        volume = self._qty_str(meta.key, min(float(quantity), free))
        if _dec(volume) <= 0 or _dec(volume) < meta.order_min:
            raise BrokerError(f"Position {quantity} rounds below the minimum lot for {meta.key}")
        resp = self._submit({"symbol": meta.key, "side": "SELL", "type": "MARKET",
                             "quantity": volume, "newClientOrderId": client_id_for(userref)})
        txid = self.order_id(meta.key, resp.get("orderId"))
        self._log(AuditEvent.ORDER_SUBMITTED, {"pair": meta.key, "side": "sell", "volume": volume,
                                               "order_id": txid, "userref": userref}, severity="warning")
        self._capture_fill(txid, side="sell", signal_price=float(signal_price or 0), resp=resp)
        return txid

    def _translate(self, params: dict) -> tuple[dict, dict | None]:
        """Kraken ``AddOrder`` params -> (Binance order params, stop child or None)."""
        p = dict(params)
        pair = self._pair_for(str(p.get("pair") or self.settings.symbol))
        side = str(p.get("type", "buy")).upper()
        ordertype = str(p.get("ordertype", "market")).lower()
        oflags = str(p.get("oflags", "")).lower()
        out: dict[str, Any] = {"symbol": pair, "side": side,
                               "quantity": self._qty_str(pair, p.get("volume", 0)),
                               "newClientOrderId": client_id_for(p.get("userref"))}
        if ordertype == "market":
            out["type"] = "MARKET"
        elif ordertype == "limit":
            out["price"] = self._price_str(pair, p["price"])
            if "post" in oflags:
                out["type"] = "LIMIT_MAKER"
            else:
                out["type"] = "LIMIT"
                out["timeInForce"] = str(p.get("timeinforce", "GTC")).upper()
        elif ordertype in ("stop-loss", "take-profit"):
            out["type"] = "STOP_LOSS" if ordertype == "stop-loss" else "TAKE_PROFIT"
            out["stopPrice"] = self._price_str(pair, p["price"])
        elif ordertype in ("stop-loss-limit", "take-profit-limit"):
            out["type"] = "STOP_LOSS_LIMIT" if ordertype == "stop-loss-limit" else "TAKE_PROFIT_LIMIT"
            out["stopPrice"] = self._price_str(pair, p["price"])
            out["price"] = self._price_str(pair, p.get("price2", p["price"]))
            out["timeInForce"] = str(p.get("timeinforce", "GTC")).upper()
        else:
            raise InvalidRequestError(f"Binance gateway does not support ordertype {ordertype!r}")

        child = None
        close_type = str(p.get("close[ordertype]", "")).lower()
        if close_type:
            stop_px = p.get("close[price]")
            if stop_px is None and p.get("close[trailing]") is not None:
                # No native trailing stop on Binance.US spot: arm a fixed stop at
                # the same percentage below the entry reference.
                ref = float(p.get("price") or 0) or self.get_ticker_for(pair)["last"]
                pct = float(str(p["close[trailing]"]).rstrip("%")) / 100.0
                stop_px = ref * (1 - pct) if side == "BUY" else ref * (1 + pct)
            if stop_px is not None:
                child = {"symbol": pair, "side": "SELL" if side == "BUY" else "BUY",
                         "type": "STOP_LOSS" if close_type == "stop-loss" else "TAKE_PROFIT",
                         "stopPrice": self._price_str(pair, stop_px),
                         "newClientOrderId": client_id_for(p.get("close[userref]"))}
        return out, child

    def add_order(self, params: dict, *, signal_price: float | None = None) -> str:
        """Submit Kraken-style ``AddOrder`` params (market/limit/stop + close block)."""
        params = dict(params)
        if params.pop("leverage", None) is not None:
            self._log(AuditEvent.SAFETY_VIOLATION,
                      {"operation": "AddOrder", "reason": "leverage stripped; spot-only"},
                      severity="warning")
        if not self.order_submission_enabled:
            self._log(AuditEvent.ORDER_INTENT, {"mode": "dry_run", **params})
            return f"binance-dry-{params.get('userref', int(time.time()))}"
        order, child = self._translate(params)
        resp = self._submit(order)
        txid = self.order_id(order["symbol"], resp.get("orderId"))
        self._log(AuditEvent.ORDER_SUBMITTED, {"pair": order["symbol"], "side": order["side"].lower(),
                                               "ordertype": order["type"], "order_id": txid,
                                               "bracket": child is not None}, severity="warning")
        fill = None
        if order["type"] == "MARKET" or child is not None:
            fill = self._capture_fill(txid, side=order["side"].lower(),
                                      signal_price=float(signal_price or 0), resp=resp)
        if child is not None:
            if fill is None:
                self._log(AuditEvent.BROKER_ERROR, {"operation": "bracket_stop", "order_id": txid,
                                                    "error": "entry not filled; stop not armed"},
                          severity="warning")
            else:
                self._arm_stop(txid, child, fill)
        return txid

    def _arm_stop(self, parent: str, child: dict, fill: ExecutionFill) -> None:
        meta = self.load_metadata().get(child["symbol"], {})
        qty = fill.volume
        try:
            if child["side"] == "SELL":
                qty = min(qty, self.free_balance(str(meta.get("baseAsset", ""))))
            resp = self._submit({**child, "quantity": self._qty_str(child["symbol"], qty),
                                 "newOrderRespType": "ACK"})
            stop_id = self.order_id(child["symbol"], resp.get("orderId"))
            self._closetxid[parent] = stop_id
            self._log(AuditEvent.ORDER_SUBMITTED, {"pair": child["symbol"], "side": child["side"].lower(),
                                                   "ordertype": child["type"], "order_id": stop_id,
                                                   "parent": parent}, severity="warning")
        except BrokerError as exc:
            self._log(AuditEvent.ORDER_REJECTED, {"operation": "bracket_stop", "parent": parent,
                                                  "error": str(exc)}, severity="error")

    def cancel_order(self, txid: str) -> bool:
        if not self.order_submission_enabled:
            self._log(AuditEvent.ORDER_INTENT, {"mode": "dry_run", "cancel": txid})
            return True
        self._assert_can_submit()
        pair, oid = self._split_id(txid)
        try:
            self._signed("/api/v3/order", {"symbol": pair, "orderId": oid}, method="DELETE")
            self._log(AuditEvent.ORDER_CANCELLED, {"txid": txid})
            return True
        except BrokerError as exc:
            self._log(AuditEvent.BROKER_ERROR, {"operation": "CancelOrder", "txid": txid,
                                                "error": str(exc)}, severity="warning")
            return False

    def cancel_orders(self, order_ids: list[str]) -> bool:
        expected = {str(o) for o in order_ids if str(o)}
        if not expected:
            return False
        for oid in expected:
            self.cancel_order(oid)
        try:
            remaining = {str(o["id"]) for o in self.orders()}
        except BrokerError:
            return False
        return expected.isdisjoint(remaining)

    def cancel_attached(self, userref: int) -> int:
        """Cancel open orders tagged ``dublin-<userref .. userref+9>``."""
        if not self.order_submission_enabled:
            return 0
        refs = set(range(int(userref), int(userref) + 10))
        cancelled = 0
        try:
            book = self.orders()
        except BrokerError:
            return 0
        for o in book:
            if o.get("userref") in refs and self.cancel_order(str(o["id"])):
                cancelled += 1
        return cancelled

    def edit_order(self, txid: str, *, price: float | None = None,
                   volume: float | None = None) -> str:
        """Amend via atomic cancel-replace (Binance has no in-place edit)."""
        if not self.order_submission_enabled:
            self._log(AuditEvent.ORDER_INTENT, {"mode": "dry_run", "edit": txid,
                                                "price": price, "volume": volume})
            return txid
        self._assert_can_submit()
        pair, oid = self._split_id(txid)
        cur = self._signed("/api/v3/order", {"symbol": pair, "orderId": oid})
        params: dict[str, Any] = {
            "symbol": pair, "side": cur["side"], "type": cur["type"],
            "cancelReplaceMode": "STOP_ON_FAILURE", "cancelOrderId": oid,
            "quantity": self._qty_str(pair, volume if volume is not None
                                      else float(cur["origQty"]) - float(cur.get("executedQty") or 0)),
        }
        if cur["type"] in ("LIMIT", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT", "LIMIT_MAKER"):
            params["price"] = self._price_str(pair, price if price is not None else cur["price"])
        if cur["type"] in ("LIMIT", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"):
            params["timeInForce"] = cur.get("timeInForce") or "GTC"
        if cur["type"] in ("STOP_LOSS", "TAKE_PROFIT", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"):
            params["stopPrice"] = self._price_str(pair, price if price is not None and cur["type"] in
                                                  ("STOP_LOSS", "TAKE_PROFIT") else cur["stopPrice"])
        if cur.get("clientOrderId", "").startswith(CLIENT_PREFIX):
            params["newClientOrderId"] = cur["clientOrderId"]
        resp = self._signed("/api/v3/order/cancelReplace", params, method="POST")
        new = (resp.get("newOrderResponse") or {}).get("orderId")
        new_id = self.order_id(pair, new) if new is not None else txid
        self._log(AuditEvent.ORDER_EDITED, {"txid": txid, "new_txid": new_id})
        return new_id


    # ── Kraken-shaped compatibility layer ──────────────────
    # Callers written against Kraken (dashboard account panels, research
    # scripts) use ``_public("OHLC")`` / ``_private("Balance")``. These answer
    # in Kraken's *result* shape from Binance data. Read-only: every order
    # mutation must go through add_order/buy_notional/close_position/cancel_order,
    # where the safety gates live.
    _KRAKEN_READS = frozenset({"Balance", "BalanceEx", "TradeBalance", "OpenOrders",
                               "ClosedOrders", "QueryOrders", "TradesHistory"})

    def _kraken_public(self, endpoint: str, params: dict) -> dict:
        if endpoint == "Time":
            t = self.server_time()
            return {"unixtime": int(t), "rfc1123": ""}
        if endpoint == "OHLC":
            pair = self._pair_for(str(params.get("pair") or self.settings.symbol))
            tf = int(params.get("interval", 1) or 1)
            q: dict[str, Any] = {"symbol": pair, "interval": interval_for(tf)[1], "limit": 1000}
            if params.get("since") not in (None, ""):
                q["startTime"] = int(float(params["since"])) * 1000
            rows = self._public("/api/v3/klines", q)
            out = []
            for r in rows:
                vol = float(r[5])
                vwap = float(r[7]) / vol if vol > 0 else float(r[4])
                out.append([int(r[0]) // 1000, r[1], r[2], r[3], r[4], str(vwap), r[5], int(r[8])])
            return {pair: out, "last": out[-1][0] if out else 0}
        if endpoint == "Ticker":
            result = {}
            for raw in str(params.get("pair") or self.settings.symbol).split(","):
                pair = self._pair_for(raw)
                t = self._public("/api/v3/ticker/24hr", {"symbol": pair})
                vol, vwap, n = t.get("volume", "0"), t.get("weightedAvgPrice", "0"), int(t.get("count") or 0)
                result[pair] = {
                    "a": [t.get("askPrice"), "1", "1.000"], "b": [t.get("bidPrice"), "1", "1.000"],
                    "c": [t.get("lastPrice"), t.get("lastQty", "0")], "v": [vol, vol],
                    "p": [vwap, vwap], "t": [n, n], "l": [t.get("lowPrice"), t.get("lowPrice")],
                    "h": [t.get("highPrice"), t.get("highPrice")], "o": t.get("openPrice"),
                }
            return result
        if endpoint == "AssetPairs":
            return {m.key: {"altname": m.altname, "wsname": m.wsname, "base": m.base,
                            "quote": m.quote, "lot_decimals": m.lot_decimals,
                            "pair_decimals": m.pair_decimals, "ordermin": str(m.order_min),
                            "costmin": str(m.cost_min), "status": m.status}
                    for m in self.list_usd_pairs()}
        raise InvalidRequestError(f"Kraken public endpoint {endpoint!r} has no Binance equivalent")

    def _private(self, endpoint: str, params: dict | None = None) -> dict:
        params = dict(params or {})
        if endpoint not in self._KRAKEN_READS:
            raise SafetyLockError(
                f"Kraken private endpoint {endpoint!r} is not available on the Binance gateway; "
                "orders go through add_order/buy_notional/close_position/cancel_order only")
        self._assert_private_allowed(endpoint)
        if endpoint in ("Balance", "BalanceEx"):
            rows = self._account().get("balances", [])
            if endpoint == "Balance":
                return {b["asset"]: str(float(b.get("free") or 0) + float(b.get("locked") or 0))
                        for b in rows}
            return {b["asset"]: {"balance": str(float(b.get("free") or 0) + float(b.get("locked") or 0)),
                                 "hold_trade": str(b.get("locked") or "0")} for b in rows}
        if endpoint == "TradeBalance":
            balances = self.balances()
            cash = sum(q for a, q in balances.items() if a in STABLE_QUOTES)
            eb = self.account_equity()
            return {"eb": str(eb), "tb": str(cash), "e": str(eb), "m": "0", "n": "0"}
        if endpoint == "OpenOrders":
            return {"open": {s["id"]: s for s in (self._shape(o) for o in self.open_orders_raw())}}
        if endpoint == "ClosedOrders":
            closed: dict[str, dict] = {}
            for pair in self._trade_symbols():
                for o in self._signed("/api/v3/allOrders", {"symbol": pair, "limit": 500}) or []:
                    shaped = self._shape(o)
                    if shaped["status"] != "open":
                        shaped["closetm"] = float(o.get("updateTime") or o.get("time") or 0) / 1000.0
                        closed[shaped["id"]] = shaped
            ordered = dict(sorted(closed.items(), key=lambda kv: kv[1].get("closetm", 0), reverse=True))
            ofs = int(params.get("ofs", 0) or 0)
            page = dict(list(ordered.items())[ofs:ofs + 50])
            return {"closed": page, "count": len(ordered)}
        if endpoint == "QueryOrders":
            return {txid: self.query_order(txid) for txid in str(params.get("txid", "")).split(",") if txid}
        # TradesHistory
        trades: dict[str, dict] = {}
        for pair in self._trade_symbols():
            meta = self.load_metadata().get(pair, {})
            base, quote = meta.get("baseAsset", ""), meta.get("quoteAsset", "")
            for t in self.my_trades(pair, max_pages=5):
                price = float(t.get("price") or 0)
                trades[f"{pair}-{t.get('id')}"] = {
                    "ordertxid": self.order_id(pair, t.get("orderId")), "pair": pair,
                    "time": float(t.get("time") or 0) / 1000.0,
                    "type": "buy" if t.get("isBuyer") else "sell", "ordertype": "",
                    "price": str(price), "vol": str(t.get("qty")),
                    "cost": str(t.get("quoteQty") or float(t.get("qty") or 0) * price),
                    "fee": str(_fee_in_quote(t, base, quote, price)),
                }
        ordered = dict(sorted(trades.items(), key=lambda kv: kv[1]["time"], reverse=True))
        ofs = int(params.get("ofs", 0) or 0)
        return {"trades": dict(list(ordered.items())[ofs:ofs + 50]), "count": len(ordered)}

    # ── health ─────────────────────────────────────────────
    def health(self, *, include_freshness: bool = True) -> dict:
        status: dict[str, object] = {
            "broker": "binance",
            "venue": self._host,
            "credentials_present": self.has_credentials,
            "order_submission_enabled": self.order_submission_enabled,
            "used_weight_1m": self._used_weight,
        }
        started = time.monotonic()
        try:
            server = self.server_time()
            status["reachable"] = True
            status["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
            status["clock_skew_seconds"] = round(server - time.time(), 3)
            status["error"] = None
        except Exception as exc:
            status["reachable"] = False
            status["latency_ms"] = None
            status["clock_skew_seconds"] = None
            status["error"] = str(exc)
        if include_freshness and self.last_freshness is None and status["reachable"]:
            try:
                self.check_freshness()
            except Exception as exc:
                status.setdefault("freshness_error", str(exc))
        if self.last_freshness is not None:
            status["freshness"] = self.last_freshness.to_dict()
        return status


def _fee_in_quote(fill: dict, base: str, quote: str, price: float) -> float:
    """Commission of one fill expressed in the quote currency (best effort)."""
    try:
        amount = float(fill.get("commission") or 0)
    except (TypeError, ValueError):
        return 0.0
    asset = str(fill.get("commissionAsset") or "")
    if not amount:
        return 0.0
    if asset == quote or asset in STABLE_QUOTES:
        return amount
    if asset == base:
        return amount * float(price or 0)
    return 0.0  # e.g. BNB-paid fees: not convertible without another quote

"""Native Kraken Spot gateway.

Design notes
------------
* **Read-only by default.** Order submission is behind three independent gates:
  ``dry_run``, ``paper_trading``/``allow_live_trading``, and an explicit
  ``allow_order_submission`` constructor argument.  All three must be aligned
  before a request can reach ``/0/private/AddOrder``.
* **Symbol resolution is data-driven.** Kraken's pair naming (``XXBTZUSD`` vs
  ``XBTUSD`` vs ``BTC/USD``) cannot be derived by string surgery; earlier
  guess-based normalization was a latent source of "unknown asset pair" errors.
  We resolve against ``/0/public/AssetPairs`` and match on altname, wsname, or
  the canonical key.
* **Every private call is signed, nonced, rate-limited, and retried** according
  to the error classification in ``errors.py``.

REST endpoints used:
  GET  /0/public/Time         server time (clock-skew detection)
  GET  /0/public/AssetPairs   pair metadata: precision, ordermin, costmin
  GET  /0/public/Ticker       bid/ask/last/volume
  GET  /0/public/OHLC         candles
  POST /0/private/Balance     asset balances
  POST /0/private/TradeBalance account equity
  POST /0/private/OpenOrders  working orders
  POST /0/private/ClosedOrders order lookup by userref (idempotency recovery)
  POST /0/private/AddOrder    order submission (gated)

Signing scheme (per Kraken docs):
  API-Sign = base64( HMAC-SHA512( path + SHA256(nonce + urlencoded_body), b64decode(secret) ) )
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import fcntl
import os
import tempfile
from contextlib import contextmanager
import time
import urllib.parse
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pandas as pd
import requests

from .audit import AuditEvent, AuditLog
from .config import Settings
from .errors import (
    AuthenticationError,
    BrokerError,
    InvalidRequestError,
    RateLimitError,
    SafetyLockError,
    TransientBrokerError,
    classify_kraken_error,
)
from .execution_store import ExecutionFill, ExecutionStore, utc_now
from .freshness import FreshnessGuard, FreshnessVerdict, check_monotonic_bars
from .nonce import NonceGenerator
from .precision import PairPrecision, SizedOrder, size_order
from .ratelimit import KrakenRateLimiter, RateLimitTier

KRAKEN_API_BASE = "https://api.kraken.com"
_PUBLIC_PATH = "/0/public"
_PRIVATE_PATH = "/0/private"

VALID_INTERVALS = (1, 5, 15, 30, 60, 240, 1440, 10080, 21600)

# Common aliases → Kraken's canonical asset code.
_ASSET_ALIASES = {"BTC": "XBT", "DOGE": "XDG"}


@dataclass(frozen=True)
class SymbolMeta:
    """Resolved metadata for a tradable pair."""

    key: str            # canonical Kraken key, e.g. "XXBTZUSD"
    altname: str        # e.g. "XBTUSD"
    wsname: str         # e.g. "XBT/USD"
    base: str
    quote: str
    lot_decimals: int
    pair_decimals: int
    order_min: Decimal
    cost_min: Decimal
    status: str

    @property
    def tradable(self) -> bool:
        return self.status == "online"

    def to_precision(self) -> PairPrecision:
        return PairPrecision(
            pair=self.key,
            lot_decimals=self.lot_decimals,
            pair_decimals=self.pair_decimals,
            order_min=self.order_min,
            cost_min=self.cost_min,
        )

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "altname": self.altname,
            "wsname": self.wsname,
            "base": self.base,
            "quote": self.quote,
            "lot_decimals": self.lot_decimals,
            "pair_decimals": self.pair_decimals,
            "order_min": str(self.order_min),
            "cost_min": str(self.cost_min),
            "status": self.status,
        }


@contextmanager
def private_request_lock(api_key):
    """Same OS user + API key, independent of checkout/nonce-file location.

    flock is held from before nonce allocation through response decoding. Never
    unlink this lock inode, and never reset the separate nonce high-water ledger.
    Other signers must adopt this same lock protocol before deployment.
    """
    directory = Path.home() / ".dublin" / "private-request-locks"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / (hashlib.sha256(api_key.encode()).hexdigest() + ".lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield path.with_suffix(".nonce")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class KrakenGateway:
    """Kraken Spot adapter implementing the BrokerGateway protocol."""

    def __init__(
        self,
        settings: Settings,
        *,
        session: requests.Session | None = None,
        rate_limiter: KrakenRateLimiter | None = None,
        nonce_generator: NonceGenerator | None = None,
        audit: AuditLog | None = None,
        allow_order_submission: bool = False,
        max_retries: int = 3,
        sleep_fn=time.sleep,
    ) -> None:
        self.settings = settings
        self._api_key = settings.kraken_api_key
        self._api_secret = settings.kraken_api_secret
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": "DublinBot/0.2"})
        self._limiter = rate_limiter or KrakenRateLimiter(
            _tier_from_name(getattr(settings, "kraken_tier", "starter"))
        )
        self._nonce = nonce_generator or NonceGenerator(Path(settings.nonce_state_path))
        self._audit = audit
        self._allow_order_submission = allow_order_submission
        self._max_retries = max_retries
        self._sleep = sleep_fn
        self._timeout = settings.http_timeout_seconds
        self._executions = ExecutionStore(settings.execution_db_path)
        self.last_fill: ExecutionFill | None = None

        self._meta: dict[str, SymbolMeta] = {}
        self._meta_loaded_at: float = 0.0
        self._meta_ttl = 3600.0
        self.freshness = FreshnessGuard(
            settings.timeframe_minutes,
            max_bar_age_multiple=settings.max_bar_age_multiple,
            max_clock_skew_seconds=settings.max_clock_skew_seconds,
        )
        self.last_freshness: FreshnessVerdict | None = None

    # ── introspection ────────────────────────────────────────

    @property
    def name(self) -> str:
        return "kraken"

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

    def _log(self, event: AuditEvent, payload: dict, severity: str = "info") -> None:
        if self._audit is not None:
            self._audit.record(event, payload, severity=severity)

    # ── transport ────────────────────────────────────────────

    def _handle_payload(self, payload: dict) -> dict:
        errors = payload.get("error") or []
        if errors:
            raise classify_kraken_error(list(errors))
        return payload.get("result", {})

    def _request(self, method: str, url: str, *, params=None, data=None,
                 headers=None) -> dict:
        try:
            if method == "GET":
                response = self._session.get(
                    url, params=params, timeout=self._timeout, headers=headers
                )
            else:
                response = self._session.post(
                    url, data=data, timeout=self._timeout, headers=headers
                )
        except requests.Timeout as exc:
            raise TransientBrokerError(f"timeout calling {url}") from exc
        except requests.RequestException as exc:
            raise TransientBrokerError(f"network error calling {url}: {exc}") from exc

        status = getattr(response, "status_code", 200)
        if status == 429:
            raise RateLimitError("HTTP 429 from Kraken")
        if status in (500, 502, 503, 504):
            raise TransientBrokerError(f"HTTP {status} from Kraken")
        if status >= 400:
            raise InvalidRequestError(f"HTTP {status} from Kraken")

        try:
            payload = response.json()
        except ValueError as exc:
            raise BrokerError("Kraken returned a non-JSON response") from exc
        return self._handle_payload(payload)

    def _with_retries(self, description: str, call):
        """Retry transient failures with exponential backoff; fail fast otherwise."""
        delay = 1.0
        last: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                return call()
            except (RateLimitError, TransientBrokerError) as exc:
                last = exc
                if isinstance(exc, RateLimitError):
                    self._log(
                        AuditEvent.RATE_LIMIT,
                        {"operation": description, "attempt": attempt},
                        severity="warning",
                    )
                if attempt == self._max_retries:
                    break
                self._sleep(delay)
                delay *= 2
            except AuthenticationError:
                # Never retry auth failures: a bad nonce or key is not fixed by
                # repetition and repeated failures can trigger key lockout.
                raise
        assert last is not None
        raise last

    def _public(self, endpoint: str, params: dict | None = None) -> dict:
        self._limiter.acquire_public()
        url = f"{KRAKEN_API_BASE}{_PUBLIC_PATH}/{endpoint}"
        return self._with_retries(
            f"public:{endpoint}",
            lambda: self._request("GET", url, params=params),
        )

    def _sign(self, urlpath: str, data: dict) -> str:
        encoded = (str(data["nonce"]) + urllib.parse.urlencode(data)).encode()
        message = urlpath.encode() + hashlib.sha256(encoded).digest()
        try:
            secret = base64.b64decode(self._api_secret)
        except Exception as exc:
            raise AuthenticationError("Kraken API secret is not valid base64") from exc
        mac = hmac.new(secret, message, hashlib.sha512)
        return base64.b64encode(mac.digest()).decode()

    def _private(self, endpoint: str, params: dict | None = None) -> dict:
        if not self.has_credentials:
            raise AuthenticationError(
                "Kraken credentials required for private endpoints"
            )
        urlpath = f"{_PRIVATE_PATH}/{endpoint}"
        url = f"{KRAKEN_API_BASE}{urlpath}"

        def call() -> dict:
            self._limiter.acquire_private(endpoint)
            with private_request_lock(self._api_key) as highwater:
                # A fresh nonce per attempt is mandatory: replaying a nonce after a
                # timeout is itself an EAPI:Invalid nonce failure.
                body = dict(params or {})
                previous = int(highwater.read_text()) if highwater.exists() else 0
                if previous < 0:
                    raise AuthenticationError("Corrupt shared nonce watermark")
                nonce = max(self._nonce.next(), previous + 1)
                # Preserve the configured NonceGenerator ledger AND add a
                # key-scoped high-water for signers in different checkouts.
                # Persist before sending; any I/O failure prevents dispatch.
                fd, name = tempfile.mkstemp(dir=highwater.parent, prefix=".nonce-")
                try:
                    with os.fdopen(fd, "w") as out:
                        out.write(str(nonce))
                        out.flush()
                        os.fsync(out.fileno())
                    os.replace(name, highwater)
                    directory = os.open(highwater.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                finally:
                    if os.path.exists(name):
                        os.unlink(name)
                body["nonce"] = str(nonce)
                headers = {
                    "API-Key": self._api_key,
                    "API-Sign": self._sign(urlpath, body),
                    "Content-Type": "application/x-www-form-urlencoded",
                }
                return self._request("POST", url, data=body, headers=headers)

        # Unknown endpoints are mutations until explicitly classified read-only.
        reads = {"Balance", "BalanceEx", "TradeBalance", "OpenOrders", "ClosedOrders",
                 "QueryOrders", "TradesHistory", "QueryTrades", "Ledgers", "QueryLedgers",
                 "TradeVolume", "OpenPositions", "GetApiKeyInfo"}
        return self._with_retries(f"private:{endpoint}", call) if endpoint in reads else call()

    # ── symbol metadata ──────────────────────────────────────

    def load_metadata(self, force: bool = False) -> dict[str, SymbolMeta]:
        if self._meta and not force and (time.time() - self._meta_loaded_at) < self._meta_ttl:
            return self._meta
        result = self._public("AssetPairs")
        meta: dict[str, SymbolMeta] = {}
        for key, info in result.items():
            if not isinstance(info, dict):
                continue
            meta[key] = SymbolMeta(
                key=key,
                altname=info.get("altname", key),
                wsname=info.get("wsname", ""),
                base=info.get("base", ""),
                quote=info.get("quote", ""),
                lot_decimals=int(info.get("lot_decimals", 8)),
                pair_decimals=int(info.get("pair_decimals", 8)),
                order_min=Decimal(str(info.get("ordermin", "0"))),
                cost_min=Decimal(str(info.get("costmin", "0"))),
                status=info.get("status", "online"),
            )
        self._meta = meta
        self._meta_loaded_at = time.time()
        return meta

    @staticmethod
    def _candidates(symbol: str) -> list[str]:
        """Plausible spellings of a configured symbol, for metadata matching."""
        raw = symbol.upper().strip()
        compact = raw.replace("/", "").replace("-", "").replace("_", "")
        options = {raw, compact}
        if "/" in raw:
            base, _, quote = raw.partition("/")
            base_alias = _ASSET_ALIASES.get(base, base)
            quote_alias = _ASSET_ALIASES.get(quote, quote)
            options.update({
                f"{base_alias}{quote_alias}",
                f"{base_alias}/{quote_alias}",
            })
        for src, dst in _ASSET_ALIASES.items():
            if compact.startswith(src):
                options.add(dst + compact[len(src):])
        return [o for o in options if o]

    def list_usd_pairs(self) -> list[SymbolMeta]:
        """Every active Kraken */USD spot pair — the full tradeable universe.

        Used by the autonomous selector (universe_mode="all_usd") so the bot can
        discover and trade any USD-quoted coin Kraken offers, not just a fixed
        basket. Pairs that are delisted/suspended are excluded by status.

        NOTE: Kraken's metadata reports the USD quote as ``ZUSD`` (and other
        assets carry a ``Z``/``X`` prefix, e.g. ``XXBT``, ``ZEUR``). We match
        both the plain and the prefixed form so the USD universe is complete.
        """
        meta = self.load_metadata()
        out: list[SymbolMeta] = []
        for m in meta.values():
            quote = str(m.quote).upper().lstrip("Z")
            if quote == "USD" and str(m.status).lower() == "online":
                out.append(m)
        return out

    def resolve_symbol(self, symbol: str | None = None) -> SymbolMeta:
        """Resolve a configured symbol to authoritative Kraken pair metadata."""
        symbol = symbol or self.settings.symbol
        meta = self.load_metadata()
        candidates = self._candidates(symbol)
        for candidate in candidates:
            if candidate in meta:
                return meta[candidate]
        for candidate in candidates:
            for entry in meta.values():
                if candidate in (entry.altname.upper(), entry.wsname.upper()):
                    return entry
        raise InvalidRequestError(
            f"Kraken has no asset pair matching {symbol!r} "
            f"(tried: {', '.join(sorted(candidates))})"
        )

    @property
    def pair(self) -> str:
        """Canonical Kraken pair key for the configured symbol."""
        return self.resolve_symbol().key

    def asset_info(self, symbol: str | None = None) -> SymbolMeta | None:
        try:
            return self.resolve_symbol(symbol)
        except InvalidRequestError:
            return None

    # ── market data ──────────────────────────────────────────

    def kraken_interval(self) -> int:
        mins = self.settings.timeframe_minutes
        if mins in VALID_INTERVALS:
            return mins
        return min(VALID_INTERVALS, key=lambda v: abs(v - mins))

    def server_time(self) -> float:
        return float(self._public("Time")["unixtime"])

    def get_bars(self, *, validate: bool = True) -> pd.DataFrame:
        """Fetch OHLC candles, dropping the in-progress final bar.

        Kraken's OHLC response always ends with the *currently forming* candle.
        Feeding it to the strategy causes look-ahead-style instability, because
        the indicator values change on every poll within the same interval.
        """
        meta = self.resolve_symbol()
        result = self._public(
            "OHLC", {"pair": meta.key, "interval": self.kraken_interval()}
        )
        rows = result.get(meta.key) or result.get(meta.altname) or []
        if not rows:
            # Kraken occasionally keys the result by a name other than the one
            # requested; fall back to the single non-"last" entry.
            for key, value in result.items():
                if key != "last" and isinstance(value, list) and value:
                    rows = value
                    break
        if not rows:
            raise BrokerError(f"No OHLC data returned for {meta.key}")

        timestamps = [pd.Timestamp(int(r[0]), unit="s", tz="UTC") for r in rows]
        frame = pd.DataFrame(
            [
                {
                    "open": float(r[1]),
                    "high": float(r[2]),
                    "low": float(r[3]),
                    "close": float(r[4]),
                    "vwap": float(r[5]),
                    "volume": float(r[6]),
                    "trades": int(r[7]),
                }
                for r in rows
            ],
            index=pd.DatetimeIndex(timestamps, name="timestamp"),
        ).sort_index()

        if len(frame) > 1:
            frame = frame.iloc[:-1]  # drop the still-forming candle

        if validate:
            check_monotonic_bars(frame)
        return frame.tail(self.settings.lookback_bars)

    def get_ticker(self) -> dict:
        meta = self.resolve_symbol()
        return self.get_ticker_for(meta.altname)

    def get_ticker_for(self, symbol: str) -> dict:
        """Ticker for an arbitrary symbol (used by symbol auto-selection)."""
        meta = self.resolve_symbol(symbol)
        result = self._public("Ticker", {"pair": meta.key})
        raw = result.get(meta.key) or result.get(meta.altname)
        if raw is None and result:
            raw = next(iter(result.values()))
        if not raw:
            raise BrokerError(f"No ticker data for {meta.key}")
        return {
            "bid": float(raw["b"][0]),
            "ask": float(raw["a"][0]),
            "last": float(raw["c"][0]),
            "volume_24h": float(raw["v"][1]),
            "vwap_24h": float(raw["p"][1]),
            "trades_24h": int(raw["t"][1]),
        }

    def check_freshness(self, bars: pd.DataFrame | None = None) -> FreshnessVerdict:
        """Validate feed freshness against bar age *and* exchange clock skew."""
        try:
            server = self.server_time()
        except BrokerError:
            server = None
        frame = self.get_bars() if bars is None else bars
        verdict = self.freshness.evaluate_bars(frame, server_time=server)
        self.last_freshness = verdict
        self._log(
            AuditEvent.DATA_FRESHNESS,
            verdict.to_dict(),
            severity="info" if verdict.fresh else "warning",
        )
        return verdict

    def market_quality(self) -> dict:
        ticker = self.get_ticker()
        bid, ask = ticker["bid"], ticker["ask"]
        mid = (bid + ask) / 2
        return {
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "spread_bps": ((ask - bid) / mid) * 10_000 if mid > 0 else float("inf"),
            "recent_dollar_volume": ticker["volume_24h"] * ticker["vwap_24h"],
        }

    # ── account ──────────────────────────────────────────────

    def balances(self) -> dict[str, float]:
        if not self.has_credentials:
            return {}
        result = self._private("Balance")
        return {asset: float(amount) for asset, amount in result.items()}

    def account_equity(self) -> float:
        """Total equity in the account's base currency."""
        if not self.has_credentials:
            return self.settings.strategy_equity_usd
        try:
            result = self._private("TradeBalance", {"asset": "ZUSD"})
            return float(result.get("eb", 0.0))
        except BrokerError as exc:
            self._log(
                AuditEvent.BROKER_ERROR,
                {"operation": "account_equity", "error": str(exc)},
                severity="warning",
            )
            return self.settings.strategy_equity_usd

    def positions(self) -> list[dict]:
        """Spot 'positions' derived from non-zero, non-quote asset balances.

        Kraken Spot has no position concept — ``/0/private/OpenPositions`` is a
        margin endpoint and returns nothing for a cash account.  Holdings are
        therefore reconstructed from balances, which is what actually reflects
        exposure on a spot account.
        """
        if not self.has_credentials:
            return []
        try:
            balances = self.balances()
            meta = self.resolve_symbol()
        except BrokerError as exc:
            self._log(
                AuditEvent.BROKER_ERROR,
                {"operation": "positions", "error": str(exc)},
                severity="warning",
            )
            return []

        base = meta.base
        quantity = float(balances.get(base, 0.0))
        if quantity <= float(meta.order_min or 0):
            return []
        try:
            price = self.get_ticker()["last"]
        except BrokerError:
            price = 0.0
        return [{
            "symbol": meta.altname,
            "asset": base,
            "side": "long",
            "quantity": quantity,
            "average_entry": 0.0,   # spot balances carry no cost basis
            "market_value": quantity * price,
            "unrealized_pl": 0.0,
        }]

    def has_position(self) -> bool:
        return bool(self.positions())

    def fee_schedule(self, pair: str | None = None) -> dict[str, float] | None:
        """Live taker/maker fee in BPS from Kraken ``TradeVolume``.

        Kraken's fee depends on 30d volume, so it drifts as the account trades.
        Backtests and the paper fill model must use the REAL number — a stale
        or assumed fee (the old hardcoded 26 bps vs an actual 80 bps) makes
        losing strategies look profitable.

        Returns ``{"taker_bps": float, "maker_bps": float}`` or ``None`` when
        the schedule cannot be read (caller should then keep its assumption).
        """
        if not self.has_credentials:
            return None
        # TradeVolume returns ``fees: null`` unless a pair is supplied, so fall
        # back to the configured symbol's canonical pair.
        if not pair:
            try:
                pair = self.resolve_symbol().key
            except Exception:
                return None
        try:
            result = self._private("TradeVolume", {"pair": pair})
        except BrokerError:
            return None
        fees = result.get("fees") or {}
        maker = result.get("fees_maker") or {}
        if not isinstance(fees, dict) or not fees:
            return None

        # Each entry is keyed by pair (or "default"): {"fee": "0.8000", ...}
        def _first_bps(table: dict) -> float | None:
            for _, entry in table.items():
                if not isinstance(entry, dict):
                    continue
                raw = entry.get("fee")
                if raw is None:
                    continue
                try:
                    return float(raw) * 100.0  # percent -> bps
                except (TypeError, ValueError):
                    continue
            return None

        taker_bps = _first_bps(fees)
        maker_bps = _first_bps(maker) if maker else None
        if taker_bps is None:
            return None
        return {
            "taker_bps": taker_bps,
            "maker_bps": maker_bps if maker_bps is not None else taker_bps,
        }

    def closed_trade_pnl(self, since: int = 0) -> tuple[list[dict], int]:
        """Realized P&L of closed trades since ``since`` (unix SECONDS).

        Used by the self-learning agent to ingest REAL closed-trade outcomes in
        live mode (where the paper portfolio does not track fills). Returns the
        list of individual trades (each carrying ``txid``, ``symbol``, ``pnl``,
        and the trade's unix-SECONDS timestamp) plus the latest trade timestamp
        to use as the next ``since`` cursor.

        Both values are in SECONDS — Kraken's ``TradesHistory`` ``start`` and
        ``time`` fields are seconds. The previous implementation multiplied the
        cursor by 1e9 (treating it as nanoseconds), which made every cursor
        after the first run absurdly large and silently stopped all
        re-ingestion. Returning per-trade rows (not a pre-aggregated per-symbol
        sum) lets the learner de-duplicate by txid so a re-fetch after a restart
        never double-counts realized P&L.

        Raises BrokerError/AuthenticationError on failure — callers must guard.
        """
        result = self._private("TradesHistory", {"type": "all", "trades": True,
                                                 "start": str(since)})
        trades = result.get("trades") or {}
        rows: list[dict] = []
        latest = since
        for txid, t in trades.items():
            sym = str(t.get("pair", "")).replace("/", "").upper()
            # Kraken reports closed-trade realized P&L in the "pnl" field.
            raw = t.get("pnl")
            if raw is None:
                continue
            try:
                val = float(raw)
            except (TypeError, ValueError):
                continue
            try:
                ts = int(t.get("time", 0))
            except (TypeError, ValueError):
                ts = 0
            latest = max(latest, ts)
            rows.append({"txid": str(txid), "symbol": sym, "pnl": val, "ts": ts})
        return rows, latest

    def orders(self) -> list[dict]:
        if not self.has_credentials:
            return []
        try:
            result = self._private("OpenOrders")
        except BrokerError as exc:
            self._log(
                AuditEvent.BROKER_ERROR,
                {"operation": "orders", "error": str(exc)},
                severity="warning",
            )
            return []
        orders = []
        for order_id, order in (result.get("open") or {}).items():
            descr = order.get("descr", {})
            orders.append({
                "id": order_id,
                "symbol": descr.get("pair", ""),
                "side": descr.get("type", ""),
                "order_type": descr.get("ordertype", ""),
                "status": order.get("status", "open"),
                "volume": float(order.get("vol", 0)),
                "volume_executed": float(order.get("vol_exec", 0)),
                "notional": float(order.get("cost", 0)),
                "userref": order.get("userref"),
            })
        return orders

    def find_order_by_userref(self, userref: int) -> dict | None:
        """Locate an order by idempotency userref across open and closed books.

        This is the recovery path after an ambiguous submission: it answers
        "did my order actually land?" without risking a duplicate.
        """
        if not self.has_credentials:
            return None
        for order in self.orders():
            if order.get("userref") == userref:
                return order
        try:
            result = self._private("ClosedOrders", {"userref": userref})
        except BrokerError:
            return None
        for order_id, order in (result.get("closed") or {}).items():
            if order.get("userref") == userref:
                descr = order.get("descr", {})
                return {
                    "id": order_id,
                    "symbol": descr.get("pair", ""),
                    "side": descr.get("type", ""),
                    "status": order.get("status", "closed"),
                    "volume": float(order.get("vol", 0)),
                    "volume_executed": float(order.get("vol_exec", 0)),
                    "notional": float(order.get("cost", 0)),
                    "userref": userref,
                }
        return None

    def find_children_by_userref(self, userref: int) -> list[dict]:
        """All open orders carrying ``userref`` (bracket stops/targets).

        Used to recover the exact child txids of a bracket whose ID was not
        captured at submission time (crash, partial response, or an older
        build). Cancelling by userref alone is unsafe — it can hit a
        same-symbol order we do not own — so this only ever *discovers* IDs;
        callers still cancel by explicit txid.
        """
        if not self.has_credentials:
            return []
        return [
            order for order in self.orders()
            if order.get("userref") == userref
        ]

    def query_order(self, txid: str) -> dict:
        """Return Kraken's authoritative state for one submitted order."""
        result = self._private("QueryOrders", {"txid": txid, "trades": True})
        raw = result.get(txid)
        if raw is None and len(result) == 1:
            raw = next(iter(result.values()))
        if not isinstance(raw, dict):
            raise BrokerError(f"Kraken did not return order {txid}")
        return raw

    def _capture_fill(
        self,
        txid: str,
        *,
        symbol: str,
        side: str,
        signal_price: float,
        attempts: int = 5,
    ) -> ExecutionFill | None:
        """Poll QueryOrders and persist the exchange-confirmed average fill."""
        for attempt in range(max(1, attempts)):
            try:
                raw = self.query_order(txid)
            except BrokerError as exc:
                if attempt + 1 == attempts:
                    self._log(
                        AuditEvent.BROKER_ERROR,
                        {"operation": "QueryOrders", "order_id": txid, "error": str(exc)},
                        severity="warning",
                    )
                    return None
                self._sleep(0.25)
                continue

            volume = float(raw.get("vol_exec", 0) or 0)
            cost = float(raw.get("cost", 0) or 0)
            price = float(raw.get("price", 0) or 0)
            if price <= 0 and volume > 0:
                price = cost / volume
            status = str(raw.get("status", "unknown"))
            if volume > 0 and price > 0:
                expected = float(signal_price or 0)
                signed_slippage = price - expected if side == "buy" else expected - price
                slippage_bps = (
                    signed_slippage / expected * 10_000 if expected > 0 else 0.0
                )
                fill = ExecutionFill(
                    order_id=txid,
                    symbol=symbol,
                    side=side,
                    status=status,
                    signal_price=expected,
                    fill_price=price,
                    slippage_usd=signed_slippage,
                    slippage_bps=slippage_bps,
                    volume=volume,
                    cost=cost,
                    fee=float(raw.get("fee", 0) or 0),
                    filled_at=utc_now(),
                )
                self._executions.save(fill)
                self.last_fill = fill
                self._log(AuditEvent.ORDER_FILLED, fill.to_dict())
                return fill
            if status in {"canceled", "expired"}:
                return None
            if attempt + 1 < attempts:
                self._sleep(0.25)
        self._log(
            AuditEvent.BROKER_ERROR,
            {"operation": "QueryOrders", "order_id": txid,
             "error": "fill not confirmed before polling deadline"},
            severity="warning",
        )
        return None

    # ── order sizing ─────────────────────────────────────────

    def size_buy(self, notional_usd: float, price: float | None = None) -> SizedOrder:
        """Validate a USD notional against live precision/minimum constraints."""
        meta = self.resolve_symbol()
        if price is None:
            price = self.get_ticker()["ask"]
        return size_order(
            notional_usd,
            price,
            meta.to_precision(),
            min_notional_usd=self.settings.min_order_notional_usd,
        )

    # ── order execution (triple-gated) ───────────────────────

    def _assert_can_submit(self) -> None:
        if not self.order_submission_enabled:
            raise SafetyLockError(
                "Live order submission is disabled. Required: dry_run=false, "
                "paper_trading=false, allow_live_trading=true, a valid "
                "acknowledgement, and allow_order_submission=true on the gateway."
            )

    def buy_notional(self, notional_usd: float, *, userref: int | None = None,
                     signal_price: float | None = None) -> str:
        """Buy ``notional_usd`` of the configured pair.

        Orders are always plain spot orders. In dry-run the order is fully
        sized and validated against real exchange
        metadata — the only step skipped is the network call.
        """
        sized = self.size_buy(notional_usd)
        if not self.order_submission_enabled:
            self._log(AuditEvent.ORDER_INTENT, {
                "mode": "dry_run", "pair": sized.pair, "side": "buy",
                "volume": sized.volume_str, "price": sized.price_str,
                "notional": str(sized.notional), "userref": userref,
            })
            return f"kraken-dry-buy-{userref or int(time.time())}"

        self._assert_can_submit()
        params = {
            "pair": sized.pair,
            "type": "buy",
            "ordertype": "market",
            "volume": sized.volume_str,
        }
        if userref is not None:
            params["userref"] = str(userref)
        result = self._private("AddOrder", params)
        order_id = (result.get("txid") or ["unknown"])[0]
        self._log(AuditEvent.ORDER_SUBMITTED, {
            "pair": sized.pair, "side": "buy", "volume": sized.volume_str,
            "order_id": order_id, "userref": userref,
        }, severity="warning")
        self._capture_fill(
            order_id,
            symbol=sized.pair,
            side="buy",
            signal_price=float(signal_price or sized.price),
        )
        return order_id

    def margin_positions(self) -> list[dict]:
        """Open margin positions (Kraken ``OpenPositions`` endpoint).

        Returns [] when the account has no margin positions or the call fails.
        Each entry carries the pair, side, volume, leverage, and margin total so
        the engine can enforce the margin exposure cap and the liquidation guard.
        """
        if not self.has_credentials:
            return []
        try:
            result = self._private("OpenPositions")
        except BrokerError as exc:
            self._log(AuditEvent.BROKER_ERROR,
                      {"operation": "margin_positions", "error": str(exc)},
                      severity="warning")
            return []
        positions = []
        for pair, p in (result.get("open") or {}).items():
            try:
                positions.append({
                    "pair": pair,
                    "side": "long" if float(p.get("vol", 0)) > 0 else "short",
                    "volume": abs(float(p.get("vol", 0))),
                    "leverage": float(p.get("leverage", 0) or 0),
                    "margin_total": float(p.get("margin", 0)),
                    "cost": float(p.get("cost", 0)),
                    "pnl": float(p.get("net", 0)),
                })
            except (TypeError, ValueError):
                continue
        return positions

    def close_position(self, *, userref: int | None = None,
                       quantity: float | None = None,
                       signal_price: float | None = None) -> str:
        """Sell the base-asset balance of the configured pair.

        If ``quantity`` is given it is an explicit cap (the bot's own acquired
        lot) so the order never liquidates holdings the bot did not purchase.
        Otherwise the full balance is sold (legacy behaviour).

        In paper/dry-run (``order_submission_enabled`` False), paper lots live
        only in ``paper_portfolio`` / ``paper_bot_positions.json`` — never call
        live ``positions()``; require an explicit positive quantity and return
        a synthetic order id.
        """
        # Hard no-sweep defense: a None/non-positive quantity would liquidate
        # the ENTIRE balance. Only sell an explicit lot the caller authorized.
        if quantity is None or quantity <= 0:
            raise BrokerError(
                "Refusing to close position with quantity=None (would sweep full balance)"
            )

        # Paper/dry-run short-circuit: do not consult live Kraken balances.
        if not self.order_submission_enabled:
            meta = self.resolve_symbol()
            self._log(AuditEvent.ORDER_INTENT, {
                "mode": "dry_run", "pair": meta.key, "side": "sell",
                "volume": format(float(quantity), "f"), "userref": userref,
            })
            return f"kraken-dry-close-{userref or int(time.time())}"

        positions = self.positions()
        if not positions:
            raise BrokerError("No position to close")
        meta = self.resolve_symbol()
        full = float(positions[0]["quantity"])
        target = min(quantity, full)
        precision = meta.to_precision()
        from .precision import round_volume

        volume = round_volume(target, precision)
        if volume <= 0:
            raise BrokerError(
                f"Position {target} rounds to zero volume for {meta.key}"
            )

        self._assert_can_submit()
        params = {
            "pair": meta.key,
            "type": "sell",
            "ordertype": "market",
            "volume": format(volume, "f"),
        }
        if userref is not None:
            params["userref"] = str(userref)
        result = self._private("AddOrder", params)
        order_id = (result.get("txid") or ["unknown"])[0]
        self._log(AuditEvent.ORDER_SUBMITTED, {
            "pair": meta.key, "side": "sell", "volume": format(volume, "f"),
            "order_id": order_id, "userref": userref,
        }, severity="warning")
        self._capture_fill(
            order_id,
            symbol=meta.key,
            side="sell",
            signal_price=float(signal_price or 0),
        )
        return order_id

    def add_order(self, params: dict, *, signal_price: float | None = None) -> str:
        """Submit a raw ``AddOrder`` with pre-built params (advanced orders).

        ``params`` is the flattened dict from ``orders.BracketPlan.to_addorder_params``
        (pair/type/ordertype/volume/price/close[...]). All safety gates apply:
        dry-run returns a synthetic id and logs; live requires submission enabled.
        """
        params = dict(params)
        removed_leverage = params.pop("leverage", None)
        if removed_leverage is not None:
            self._log(
                AuditEvent.SAFETY_VIOLATION,
                {"operation": "AddOrder", "reason": "leverage stripped; spot-only"},
                severity="warning",
            )
        if not self.order_submission_enabled:
            self._log(AuditEvent.ORDER_INTENT, {"mode": "dry_run", **params})
            return f"kraken-dry-{params.get('userref', int(time.time()))}"
        self._assert_can_submit()
        result = self._private("AddOrder", params)
        order_id = (result.get("txid") or ["unknown"])[0]
        self._log(AuditEvent.ORDER_SUBMITTED,
                  {"pair": params.get("pair"), "side": params.get("type"),
                   "ordertype": params.get("ordertype"), "order_id": order_id,
                   "bracket": "close" in params},
                  severity="warning")
        if str(params.get("ordertype", "")).lower() == "market":
            self._capture_fill(
                order_id,
                symbol=str(params.get("pair", self.settings.symbol)),
                side=str(params.get("type", "")),
                signal_price=float(signal_price or 0),
            )
        return order_id

    def cancel_order(self, txid: str) -> bool:
        """Cancel an open order by txid. Best-effort; returns success."""
        if not self.order_submission_enabled:
            self._log(AuditEvent.ORDER_INTENT, {"mode": "dry_run", "cancel": txid})
            return True
        self._assert_can_submit()
        try:
            self._private("CancelOrder", {"txid": txid})
            self._log(AuditEvent.ORDER_CANCELLED, {"txid": txid})
            return True
        except BrokerError as exc:
            self._log(AuditEvent.BROKER_ERROR,
                      {"operation": "CancelOrder", "txid": txid, "error": str(exc)},
                      severity="warning")
            return False

    def cancel_orders(self, order_ids: list[str]) -> bool:
        """Cancel only the supplied orders and confirm none still reserve funds.

        Bracket exits must use exact Kraken transaction IDs, not a userref range:
        userrefs are caller-controlled and may overlap an owner's other orders.
        An order that fills while cancellation is in flight is also acceptable,
        because it no longer appears in ``OpenOrders`` and cannot reserve funds.
        """
        expected = {str(order_id) for order_id in order_ids if str(order_id)}
        if not expected:
            return False
        for order_id in expected:
            self.cancel_order(order_id)
        try:
            remaining = {str(order.get("id")) for order in self.orders()}
        except BrokerError:
            return False
        return expected.isdisjoint(remaining)

    def cancel_attached(self, userref: int) -> int:
        """Cancel any open orders carrying a userref >= base and < base+10.

        Bracket children use refs ``userref+1`` (stop) and ``userref+2`` (target),
        so cancelling the parent's neighbourhood clears the whole bracket. Returns
        the number of orders cancelled.
        """
        if not self.order_submission_enabled:
            return 0
        cancelled = 0
        for ref in range(userref, userref + 10):
            try:
                result = self._private("OpenOrders", {"userref": ref})
            except BrokerError:
                continue
            for txid in (result.get("open") or {}).keys():
                if self.cancel_order(txid):
                    cancelled += 1
        return cancelled

    def edit_order(self, txid: str, *, price: float | None = None,
                   volume: float | None = None) -> str:
        """Amend an open order's price and/or volume (Kraken EditOrder)."""
        from .orders import fmt_price
        if not self.order_submission_enabled:
            self._log(AuditEvent.ORDER_INTENT,
                      {"mode": "dry_run", "edit": txid, "price": price, "volume": volume})
            return txid
        self._assert_can_submit()
        params: dict[str, str] = {"txid": txid}
        if price is not None:
            params["price"] = fmt_price(price)
        if volume is not None:
            params["volume"] = format(volume, "f")
        result = self._private("EditOrder", params)
        new_id = (result.get("txid") or [txid])[0]
        self._log(AuditEvent.ORDER_EDITED, {"txid": txid, "new_txid": new_id})
        return new_id

    # ── health ───────────────────────────────────────────────

    def health(self, *, include_freshness: bool = True) -> dict:
        """Connection/health snapshot for the dashboard.

        ``include_freshness`` performs a real freshness evaluation when none has
        been cached yet, so a freshly started dashboard shows a verdict rather
        than a blank card. It costs one OHLC call, which the dashboard caches.
        """
        status: dict[str, object] = {
            "broker": "kraken",
            "credentials_present": self.has_credentials,
            "order_submission_enabled": self.order_submission_enabled,
            "rate_limiter": self._limiter.snapshot(),
            "last_nonce": self._nonce.last,
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


def _tier_from_name(name: str) -> RateLimitTier:
    return {
        "starter": RateLimitTier.starter(),
        "intermediate": RateLimitTier.intermediate(),
        "pro": RateLimitTier.pro(),
    }.get(str(name).lower(), RateLimitTier.starter())

"""tastytrade Open API transport — OAuth2, retries, typed errors.

This module only moves bytes.  It holds **no trading policy**: whether an order
may be sent is decided by the gateway / sleeve, which call ``submit_order`` only
after every safety lock is open.  Rules taken from the official docs
(developer.tastytrade.com, read 2026-11-21):

* Sandbox ``https://api.cert.tastyworks.com`` is the default.  Production
  ``https://api.tastyworks.com`` must be selected explicitly
  (``TASTYTRADE_ENV=production``).
* OAuth2: a long-lived refresh token + client secret are exchanged at
  ``POST /oauth/token`` for a 15-minute access token.  We refresh ~60 s early.
* Every request carries ``User-Agent: product/version`` (requests without one
  are rejected with an HTML 401).
* Order placement is **not idempotent** and the API does not deduplicate.  So
  ``submit_order`` is never retried automatically; after an uncertain failure
  the caller must search for its ``external-identifier`` first
  (``find_order_by_external_id``).
* GETs retry on 429 / 5xx / network errors with capped exponential backoff.
* Errors are classified on ``error.code``, never on message text.

Secrets (refresh token, client secret, access token) are never logged.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from .errors import AuthenticationError, BrokerError, RateLimitError, TransientBrokerError

USER_AGENT = "mayo-bot/0.2"
HOSTS = {
    "sandbox": "https://api.cert.tastyworks.com",
    "production": "https://api.tastyworks.com",
}
BACKTESTER_HOST = "https://backtester.vast.tastyworks.com"
_REFRESH_MARGIN_S = 60.0


class TastytradeAPIError(BrokerError):
    """A JSON error body from tastytrade. ``code`` is the stable identifier."""

    def __init__(self, status: int, code: str, message: str, errors: list | None = None):
        super().__init__(f"tastytrade {status} {code}: {message}")
        self.status = status
        self.code = code
        self.errors = errors or []


class UncertainSubmissionError(TransientBrokerError):
    """An order POST timed out or hit a 5xx. It may or may not exist server-side.

    Never resubmit blindly — look the order up by external identifier first.
    """


def resolve_host(env: str) -> str:
    env = (env or "sandbox").strip().lower()
    if env in ("prod", "live"):
        env = "production"
    if env not in HOSTS:
        raise ValueError(f"TASTYTRADE_ENV must be 'sandbox' or 'production', got {env!r}")
    return HOSTS[env]


@dataclass
class _Token:
    value: str = ""
    expires_at: float = 0.0

    def valid(self, now: float) -> bool:
        return bool(self.value) and now < self.expires_at - _REFRESH_MARGIN_S


@dataclass
class TastytradeClient:
    refresh_token: str = ""
    client_secret: str = ""
    env: str = "sandbox"
    timeout: float = 15.0
    max_retries: int = 3
    session: Any = None
    sleep_fn: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.time
    _token: _Token = field(default_factory=_Token, repr=False)

    def __post_init__(self) -> None:
        self.base_url = resolve_host(self.env)
        self.session = self.session or requests.Session()

    # ── auth ──────────────────────────────────────────────
    @property
    def has_credentials(self) -> bool:
        return bool(self.refresh_token and self.client_secret)

    @property
    def is_production(self) -> bool:
        return self.base_url == HOSTS["production"]

    def _headers(self, auth: bool = True) -> dict:
        h = {"User-Agent": USER_AGENT, "Content-Type": "application/json",
             "Accept": "application/json"}
        if auth:
            h["Authorization"] = f"Bearer {self.access_token()}"
        return h

    def access_token(self) -> str:
        now = self.clock()
        if self._token.valid(now):
            return self._token.value
        if not self.has_credentials:
            raise AuthenticationError(
                "tastytrade credentials missing: set TASTYTRADE_REFRESH_TOKEN and "
                "TASTYTRADE_CLIENT_SECRET (OAuth personal grant)")
        body = {"grant_type": "refresh_token", "refresh_token": self.refresh_token,
                "client_secret": self.client_secret}
        try:
            r = self.session.post(f"{self.base_url}/oauth/token", json=body,
                                  headers=self._headers(auth=False), timeout=self.timeout)
        except requests.RequestException as exc:
            raise TransientBrokerError(f"tastytrade token refresh failed: {type(exc).__name__}") from exc
        if r.status_code != 200:
            # Do not echo the body: it may reflect the submitted secret.
            raise AuthenticationError(f"tastytrade token refresh rejected (HTTP {r.status_code})")
        data = r.json()
        self._token = _Token(value=data["access_token"],
                             expires_at=now + float(data.get("expires_in", 900)))
        return self._token.value

    # ── transport ─────────────────────────────────────────
    @staticmethod
    def _raise_for(r) -> None:
        if 200 <= r.status_code < 300:
            return
        code, message, errors = "http_error", f"HTTP {r.status_code}", []
        try:
            err = (r.json() or {}).get("error") or {}
            code = err.get("code") or code
            message = err.get("message") or message
            errors = err.get("errors") or []
        except ValueError:
            pass
        if r.status_code == 429:
            raise RateLimitError(f"tastytrade rate limited ({code})")
        if r.status_code == 401:
            raise AuthenticationError(f"tastytrade 401 {code}")
        if r.status_code >= 500:
            raise TransientBrokerError(f"tastytrade {r.status_code} {code}: {message}")
        raise TastytradeAPIError(r.status_code, code, message, errors)

    def _backoff(self, attempt: int) -> None:
        self.sleep_fn(min(8.0, 0.5 * 2 ** attempt) + random.uniform(0, 0.25))

    def get(self, path: str, params: dict | None = None, *, base: str | None = None) -> dict:
        url = f"{base or self.base_url}{path}"
        last: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                r = self.session.get(url, params=params or {}, headers=self._headers(),
                                     timeout=self.timeout)
                if r.status_code == 401 and attempt == 0:
                    self._token = _Token()  # expired early; refresh once
                    continue
                self._raise_for(r)
                return r.json()
            except (TransientBrokerError, requests.RequestException) as exc:
                last = exc
                if attempt < self.max_retries - 1:
                    self._backoff(attempt)
        if isinstance(last, BrokerError):
            raise last
        raise TransientBrokerError(f"tastytrade GET {path} failed: {type(last).__name__}")

    def post(self, path: str, body: dict, *, base: str | None = None) -> dict:
        """Single-shot POST. Callers that need retries implement them explicitly."""
        try:
            r = self.session.post(f"{base or self.base_url}{path}", json=body,
                                  headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as exc:
            raise UncertainSubmissionError(f"tastytrade POST {path}: {type(exc).__name__}") from exc
        if r.status_code >= 500:
            raise UncertainSubmissionError(f"tastytrade POST {path}: HTTP {r.status_code}")
        self._raise_for(r)
        return r.json() if r.content else {}

    # ── accounts ──────────────────────────────────────────
    def account_numbers(self) -> list[str]:
        items = self.get("/customers/me/accounts").get("data", {}).get("items", [])
        return [str((i.get("account") or i).get("account-number")) for i in items]

    def balances(self, account: str) -> dict:
        return self.get(f"/accounts/{account}/balances").get("data", {})

    def positions(self, account: str, **filters) -> list[dict]:
        return self.get(f"/accounts/{account}/positions", filters).get("data", {}).get("items", [])

    def live_orders(self, account: str) -> list[dict]:
        return self.get(f"/accounts/{account}/orders/live").get("data", {}).get("items", [])

    def find_order_by_external_id(self, account: str, external_id: str) -> dict | None:
        for o in self.live_orders(account):
            if o.get("external-identifier") == external_id:
                return o
        return None

    # ── market data ───────────────────────────────────────
    def market_data(self, *, equity: list[str] = (), equity_option: list[str] = (),
                    cryptocurrency: list[str] = ()) -> dict[str, dict]:
        params = {}
        if equity:
            params["equity"] = ",".join(equity)
        if equity_option:
            params["equity-option"] = ",".join(equity_option)
        if cryptocurrency:
            params["cryptocurrency"] = ",".join(cryptocurrency)
        total = len(equity) + len(equity_option) + len(cryptocurrency)
        if total > 100:
            raise ValueError("tastytrade market-data is capped at 100 symbols per request")
        items = self.get("/market-data/by-type", params).get("data", {}).get("items", [])
        return {i["symbol"]: i for i in items}

    def market_metrics(self, symbols: list[str]) -> dict[str, dict]:
        items = self.get("/market-metrics", {"symbols": ",".join(symbols)}) \
            .get("data", {}).get("items", [])
        return {i["symbol"]: i for i in items}

    def nested_option_chain(self, symbol: str) -> dict:
        items = self.get(f"/option-chains/{symbol}/nested").get("data", {}).get("items", [])
        if not items:
            raise BrokerError(f"tastytrade has no option chain for {symbol}")
        # Prefer the standard chain (excludes adjusted / non-standard deliverables).
        for it in items:
            if it.get("option-chain-type", "Standard") == "Standard":
                return it
        return items[0]

    def cryptocurrency(self, symbol: str) -> dict:
        return self.get(f"/instruments/cryptocurrencies/{symbol}").get("data", {})

    # ── orders ────────────────────────────────────────────
    def dry_run_order(self, account: str, body: dict) -> dict:
        return self.post(f"/accounts/{account}/orders/dry-run", body).get("data", {})

    def submit_order(self, account: str, body: dict) -> dict:
        if not body.get("external-identifier"):
            raise ValueError("every submitted order needs an external-identifier")
        return self.post(f"/accounts/{account}/orders", body).get("data", {})

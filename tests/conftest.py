"""Shared pytest fixtures and Kraken response builders.

Every test in this suite runs fully offline: ``requests.Session`` is replaced by
``FakeSession``, which serves canned Kraken payloads.  No test may perform a
real network call or place a real order.
"""

from __future__ import annotations

import base64
import time
from typing import Any

import pandas as pd
import pytest

from dublin_bot.config import Settings

FAKE_SECRET = base64.b64encode(b"dublin-test-secret-key-material!").decode()


@pytest.fixture(autouse=True)
def isolate_runtime_files(tmp_path, monkeypatch):
    """Never let a test overwrite the live bot's logs or risk state."""
    monkeypatch.chdir(tmp_path)


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    """Minimal stand-in for ``requests.Session`` driven by a route table.

    Routes are keyed by the final path segment (``"Time"``, ``"AddOrder"``, …).
    A route value may be a payload, an ``Exception`` to raise, or a list of
    either to script a sequence of responses across retries.
    """

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.headers: dict[str, str] = {}
        self.routes: dict[str, Any] = routes or {}
        self.calls: list[dict[str, Any]] = []

    def _resolve(self, url: str, kind: str, **kwargs):
        endpoint = url.rstrip("/").split("/")[-1]
        self.calls.append({"endpoint": endpoint, "kind": kind, **kwargs})
        if endpoint not in self.routes:
            raise AssertionError(f"unmocked Kraken endpoint: {endpoint}")
        route = self.routes[endpoint]
        if isinstance(route, list):
            route = route.pop(0) if len(route) > 1 else route[0]
        if isinstance(route, Exception):
            raise route
        # Duck-typed rather than isinstance: pytest may import conftest under a
        # different module name than the test module's `from tests.conftest`
        # import, producing two distinct FakeResponse classes.
        if hasattr(route, "status_code") and hasattr(route, "json"):
            return route
        return FakeResponse(route)

    def get(self, url, params=None, timeout=None, headers=None):
        return self._resolve(url, "GET", params=params, headers=headers)

    def post(self, url, data=None, timeout=None, headers=None):
        return self._resolve(url, "POST", data=data, headers=headers)

    def endpoint_calls(self, endpoint: str) -> list[dict]:
        return [c for c in self.calls if c["endpoint"] == endpoint]


# ── Kraken payload builders ─────────────────────────────────────────

def asset_pairs_payload() -> dict:
    return {
        "error": [],
        "result": {
            "XXBTZUSD": {
                "altname": "XBTUSD",
                "wsname": "XBT/USD",
                "base": "XXBT",
                "quote": "ZUSD",
                "lot_decimals": 8,
                "pair_decimals": 1,
                "ordermin": "0.00005",
                "costmin": "0.5",
                "status": "online",
            },
            "XETHZUSD": {
                "altname": "ETHUSD",
                "wsname": "ETH/USD",
                "base": "XETH",
                "quote": "ZUSD",
                "lot_decimals": 8,
                "pair_decimals": 2,
                "ordermin": "0.002",
                "costmin": "0.5",
                "status": "online",
            },
        },
    }


def ohlc_payload(bars: int = 300, *, end_ts: float | None = None,
                 interval_minutes: int = 60, start_price: float = 50_000.0) -> dict:
    """Generate a mildly trending OHLC series ending at ``end_ts``.

    One extra bar is appended because the gateway drops the final (in-progress)
    candle, mirroring real Kraken behaviour.
    """
    end_ts = end_ts if end_ts is not None else time.time()
    step = interval_minutes * 60
    rows = []
    price = start_price
    for i in range(bars + 1):
        ts = int(end_ts - (bars - i) * step)
        price = price * 1.001
        rows.append([
            ts,
            f"{price:.1f}", f"{price * 1.004:.1f}", f"{price * 0.997:.1f}",
            f"{price * 1.002:.1f}", f"{price:.1f}", "12.5", 250,
        ])
    return {"error": [], "result": {"XXBTZUSD": rows, "last": rows[-1][0]}}


def ticker_payload(bid: float = 49_990.0, ask: float = 50_010.0,
                   last: float = 50_000.0, volume_24h: float = 1_500.0) -> dict:
    return {
        "error": [],
        "result": {
            "XXBTZUSD": {
                "a": [f"{ask}", "1", "1.0"],
                "b": [f"{bid}", "1", "1.0"],
                "c": [f"{last}", "0.01"],
                "v": ["100.0", f"{volume_24h}"],
                "p": [f"{last}", f"{last}"],
                "t": [500, 5000],
                "l": ["48000.0", "47000.0"],
                "h": ["51000.0", "52000.0"],
                "o": "49500.0",
            }
        },
    }


def time_payload(now: float | None = None) -> dict:
    return {"error": [], "result": {"unixtime": int(now if now else time.time()),
                                    "rfc1123": "Tue, 04 Aug 2026 00:00:00 GMT"}}


def balance_payload(**assets: str) -> dict:
    return {"error": [], "result": dict(assets or {"ZUSD": "1000.0000"})}


def error_payload(*codes: str) -> dict:
    return {"error": list(codes)}


def default_routes(**overrides) -> dict:
    routes = {
        "Time": time_payload(),
        "AssetPairs": asset_pairs_payload(),
        "Ticker": ticker_payload(),
        "OHLC": ohlc_payload(),
        "Balance": balance_payload(),
        "TradeBalance": {"error": [], "result": {"eb": "1000.0000", "tb": "1000.0000"}},
        "OpenOrders": {"error": [], "result": {"open": {}}},
        "ClosedOrders": {"error": [], "result": {"closed": {}}},
    }
    routes.update(overrides)
    return routes


# ── fixtures ────────────────────────────────────────────────────────

@pytest.fixture
def settings_factory(tmp_path):
    """Settings in full paper/dry-run lockdown with state paths in tmp_path."""
    def _make(**overrides) -> Settings:
        defaults = dict(
            _env_file=None,
            broker="kraken",
            paper_trading=True,
            dry_run=True,
            allow_live_trading=False,
            kraken_api_key="test-key",
            kraken_api_secret=FAKE_SECRET,
            symbol="BTC/USD",
            # Network-backed sentiment is covered by its own fully mocked
            # tests. Keep shared engine fixtures strictly offline.
            sentiment_enabled=False,
            timeframe_minutes=60,
            lookback_bars=250,
            journal_path=tmp_path / "decisions.jsonl",
            audit_log_path=tmp_path / "audit.jsonl",
            idempotency_path=tmp_path / "orders.json",
            nonce_state_path=tmp_path / "nonce.json",
            session_state_path=tmp_path / "session_state.json",
        )
        defaults.update(overrides)
        return Settings(**defaults)
    return _make


@pytest.fixture
def settings(settings_factory) -> Settings:
    return settings_factory()


@pytest.fixture
def fake_session() -> FakeSession:
    return FakeSession(default_routes())


@pytest.fixture
def gateway(settings, fake_session):
    """A Kraken gateway wired to the fake session, with no retry sleeping."""
    from dublin_bot.kraken_gateway import KrakenGateway
    return KrakenGateway(
        settings, session=fake_session, max_retries=3, sleep_fn=lambda _s: None
    )


@pytest.fixture
def synthetic_bars() -> pd.DataFrame:
    """Deterministic uptrending bars sufficient for a 200-period regime EMA."""
    periods = 320
    index = pd.date_range("2026-01-01", periods=periods, freq="1h", tz="UTC")
    closes = [100.0 * (1.002 ** i) for i in range(periods)]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.005 for c in closes],
            "low": [c * 0.996 for c in closes],
            "close": closes,
            "volume": [1000.0] * periods,
        },
        index=index,
    )

"""tastytrade transport + crypto gateway: auth, retries, and every safety gate."""

from __future__ import annotations

import pytest

from dublin_bot.audit import AuditLog
from dublin_bot.config import Settings
from dublin_bot.engine import build_gateway
from dublin_bot.errors import AuthenticationError, BrokerError, SafetyLockError
from dublin_bot.tastytrade_client import (
    HOSTS,
    TastytradeAPIError,
    TastytradeClient,
    UncertainSubmissionError,
    resolve_host,
)
from dublin_bot.tastytrade_gateway import TastytradeGateway, fee_bps_for

ACK = "I_ACCEPT_LIVE_TRADING_RISK"


# ── fake HTTP session ───────────────────────────────────────
class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.content = b"x"

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, script):
        self.script = list(script)  # list of (method, path_substring, Resp)
        self.calls = []

    def _next(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for i, (m, frag, resp) in enumerate(self.script):
            if m == method and frag in url:
                self.script.pop(i)
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"unexpected {method} {url}")

    def get(self, url, **kw):
        return self._next("GET", url, **kw)

    def post(self, url, **kw):
        return self._next("POST", url, **kw)


TOKEN = ("POST", "/oauth/token", Resp(200, {"access_token": "tok", "expires_in": 900}))


def client(script, **kw):
    return TastytradeClient(refresh_token="r", client_secret="s", session=FakeSession(script),
                            sleep_fn=lambda _s: None, **kw)


# ── client ─────────────────────────────────────────────────
def test_sandbox_is_default_and_production_is_explicit():
    assert resolve_host("") == HOSTS["sandbox"]
    assert resolve_host("production") == HOSTS["production"]
    with pytest.raises(ValueError):
        resolve_host("paper")


def test_token_is_cached_and_user_agent_always_sent():
    c = client([TOKEN, ("GET", "/market-data", Resp(200, {"data": {"items": []}})),
                ("GET", "/market-data", Resp(200, {"data": {"items": []}}))])
    c.market_data(equity=["SPY"])
    c.market_data(equity=["QQQ"])
    calls = c.session.calls
    assert sum(1 for m, u, _ in calls if "/oauth/token" in u) == 1
    assert all(kw["headers"]["User-Agent"].startswith("mayo-bot/") for _, _, kw in calls)
    assert calls[1][2]["headers"]["Authorization"] == "Bearer tok"


def test_token_refreshes_before_expiry():
    now = [1000.0]
    c = client([TOKEN, ("GET", "/x", Resp(200, {})),
                ("POST", "/oauth/token", Resp(200, {"access_token": "tok2", "expires_in": 900})),
                ("GET", "/x", Resp(200, {}))], clock=lambda: now[0])
    c.get("/x")
    now[0] += 900 - 30  # inside the 60 s refresh margin
    c.get("/x")
    assert c.session.calls[-1][2]["headers"]["Authorization"] == "Bearer tok2"


def test_missing_credentials_is_an_auth_error():
    c = TastytradeClient(session=FakeSession([]))
    with pytest.raises(AuthenticationError):
        c.get("/customers/me/accounts")


def test_get_retries_429_then_succeeds():
    c = client([TOKEN, ("GET", "/x", Resp(429, {})), ("GET", "/x", Resp(200, {"ok": 1}))])
    assert c.get("/x") == {"ok": 1}


def test_api_error_exposes_code_not_message():
    body = {"error": {"code": "invalid_symbol", "message": "nope"}}
    c = client([TOKEN, ("GET", "/x", Resp(422, body))])
    with pytest.raises(TastytradeAPIError) as e:
        c.get("/x")
    assert e.value.code == "invalid_symbol"


def test_order_post_is_never_retried_and_5xx_is_uncertain():
    c = client([TOKEN, ("POST", "/orders", Resp(502, {}))])
    with pytest.raises(UncertainSubmissionError):
        c.submit_order("ACCT", {"external-identifier": "e1", "legs": []})
    assert sum(1 for m, u, _ in c.session.calls if u.endswith("/orders")) == 1


def test_submit_requires_external_identifier():
    with pytest.raises(ValueError):
        client([TOKEN]).submit_order("ACCT", {"legs": []})


def test_market_data_symbol_cap():
    with pytest.raises(ValueError):
        client([TOKEN]).market_data(equity_option=[f"S{i}" for i in range(101)])


# ── gateway ────────────────────────────────────────────────
class FakeClient:
    is_production = True

    def __init__(self, has_credentials=True):
        self.has_credentials = has_credentials
        self.dry_runs, self.submits, self.live = [], [], []
        self.submit_error = None
        self.dry_run_errors = []

    def account_numbers(self):
        return ["5WT0001"]

    def market_data(self, **kw):
        return {"BTC/USD": {"bid": "99990", "ask": "100010", "last": "100000", "volume": "3"}}

    def dry_run_order(self, acct, body):
        self.dry_runs.append(body)
        return {"errors": self.dry_run_errors, "warnings": [],
                "fee-calculation": {"total-fees": "0.75"}}

    def submit_order(self, acct, body):
        self.submits.append(body)
        if self.submit_error:
            raise self.submit_error
        return {"order": {"id": 42}}

    def find_order_by_external_id(self, acct, ext):
        return next((o for o in self.live if o.get("external-identifier") == ext), None)

    def balances(self, acct):
        return {"net-liquidating-value": "1234.5"}

    def positions(self, acct, **kw):
        return [{"symbol": "BTC/USD", "quantity": "0.001", "quantity-direction": "Long",
                 "mark": "100000", "average-open-price": "95000"}]

    def live_orders(self, acct):
        return []


def settings(tmp_path, **kw) -> Settings:
    base = dict(broker="tastytrade", audit_log_path=tmp_path / "audit.jsonl",
                tastytrade_account_number="5WT0001")
    base.update(kw)
    return Settings(**base)


def live_settings(tmp_path, **kw):
    return settings(tmp_path, paper_trading=False, dry_run=False, allow_live_trading=True,
                    live_risk_acknowledgement=ACK, **kw)


def gw(s, fake, allow=True):
    return TastytradeGateway(s, audit=AuditLog(s.audit_log_path), client=fake,
                             allow_order_submission=allow)


def test_factory_builds_tastytrade_gateway_locked(tmp_path):
    g = build_gateway(settings(tmp_path))
    assert isinstance(g, TastytradeGateway)
    assert g.order_submission_enabled is False


def test_default_settings_never_submit(tmp_path):
    fake = FakeClient()
    oid = gw(settings(tmp_path), fake).buy_notional(25.0, userref=7)
    assert oid.startswith("tastytrade-dry-buy-")
    assert fake.submits == [] and fake.dry_runs == []


def test_all_locks_open_but_crypto_api_disabled_still_blocks(tmp_path):
    fake = FakeClient()
    g = gw(live_settings(tmp_path), fake)
    assert g.order_submission_enabled is False  # platform disable is honoured
    g.buy_notional(25.0, userref=1)
    assert fake.submits == []


@pytest.mark.parametrize("missing", ["allow", "paper_trading", "dry_run", "allow_live_trading", "ack"])
def test_every_lock_is_required(tmp_path, missing):
    kw = dict(paper_trading=False, dry_run=False, allow_live_trading=True,
              live_risk_acknowledgement=ACK, tastytrade_crypto_api_enabled=True)
    allow = True
    if missing == "allow":
        allow = False
    elif missing == "paper_trading":
        kw["paper_trading"] = True
    elif missing == "dry_run":
        kw["dry_run"] = True
    elif missing == "allow_live_trading":
        kw["allow_live_trading"] = False
        kw["paper_trading"] = True  # config refuses live without it
    elif missing == "ack":
        kw["live_risk_acknowledgement"] = "nope"
        kw["paper_trading"] = True
    g = gw(settings(tmp_path, **kw), FakeClient(), allow=allow)
    assert g.order_submission_enabled is False


def test_live_path_dry_runs_then_submits_once(tmp_path):
    fake = FakeClient()
    g = gw(live_settings(tmp_path, tastytrade_crypto_api_enabled=True), fake)
    assert g.buy_notional(25.0, userref=9) == "42"
    assert len(fake.dry_runs) == 1 and len(fake.submits) == 1
    body = fake.submits[0]
    assert body["order-type"] == "Notional Market" and body["time-in-force"] == "IOC"
    assert body["value"] == "25.00" and body["value-effect"] == "Debit"
    assert body["legs"] == [{"instrument-type": "Cryptocurrency", "symbol": "BTC/USD",
                             "action": "Buy to Open"}]
    assert body["external-identifier"] == "mayo-9"


def test_dry_run_errors_block_submission(tmp_path):
    fake = FakeClient()
    fake.dry_run_errors = [{"code": "insufficient_buying_power"}]
    g = gw(live_settings(tmp_path, tastytrade_crypto_api_enabled=True), fake)
    with pytest.raises(BrokerError):
        g.buy_notional(25.0, userref=3)
    assert fake.submits == []


def test_uncertain_submit_recovers_found_order_without_resubmitting(tmp_path, monkeypatch):
    monkeypatch.setattr("dublin_bot.tastytrade_gateway.time.sleep", lambda _s: None)
    fake = FakeClient()
    fake.submit_error = UncertainSubmissionError("timeout")
    fake.live = [{"id": 77, "external-identifier": "mayo-5"}]
    g = gw(live_settings(tmp_path, tastytrade_crypto_api_enabled=True), fake)
    assert g.buy_notional(30.0, userref=5) == "77"
    assert len(fake.submits) == 1


def test_uncertain_submit_not_found_raises_and_never_resubmits(tmp_path, monkeypatch):
    monkeypatch.setattr("dublin_bot.tastytrade_gateway.time.sleep", lambda _s: None)
    fake = FakeClient()
    fake.submit_error = UncertainSubmissionError("timeout")
    g = gw(live_settings(tmp_path, tastytrade_crypto_api_enabled=True), fake)
    with pytest.raises(UncertainSubmissionError):
        g.buy_notional(30.0, userref=6)
    assert len(fake.submits) == 1


def test_notional_limits_and_leverage_refused(tmp_path):
    g = gw(settings(tmp_path), FakeClient())
    with pytest.raises(BrokerError):
        g.build_buy_body(0.5, 1)
    with pytest.raises(BrokerError):
        g.build_buy_body(80_000, 1)
    with pytest.raises(SafetyLockError):
        g.buy_notional(10, userref=1, leverage=3)


def test_close_body_is_single_leg_sell_to_close(tmp_path):
    body = gw(settings(tmp_path), FakeClient()).build_close_body(0.0012345678912, 4)
    assert body["order-type"] == "Market" and body["time-in-force"] == "IOC"
    assert body["legs"][0]["action"] == "Sell to Close"
    assert body["legs"][0]["quantity"] == "0.00123456"


def test_live_close_uses_broker_position_quantity(tmp_path):
    fake = FakeClient()
    g = gw(live_settings(tmp_path, tastytrade_crypto_api_enabled=True), fake)
    g.close_position(userref=8)
    assert fake.submits[0]["legs"][0]["quantity"] == "0.00100000"


def test_account_reads_and_fees(tmp_path):
    g = gw(settings(tmp_path), FakeClient())
    assert g.account_equity() == 1234.5
    assert g.positions()[0]["market_value"] == 100.0
    assert fee_bps_for("BTC/USD") == 75.0 and fee_bps_for("SOL/USD") == 100.0
    mq = gw(settings(tmp_path), FakeClient(), allow=False)
    mq._bar_source = object()
    assert mq.market_quality()["fee_bps_per_side"] == 75.0


def test_health_never_raises(tmp_path):
    class Broken(FakeClient):
        def market_data(self, **kw):
            raise BrokerError("down")

    h = gw(settings(tmp_path), Broken()).health()
    assert h["reachable"] is False and "locks" in h

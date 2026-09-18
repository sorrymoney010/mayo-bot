"""End-to-end engine tests: the ordered gate pipeline.

These assert the property that matters most: **no order is ever submitted when
any gate fails**, and the safety locks cannot be bypassed by any code path
reachable from ``run_cycle``.
"""

from __future__ import annotations

import time

import pytest

from dublin_bot.audit import AuditLog
from dublin_bot.engine import TradingEngine
from dublin_bot.errors import SafetyLockError
from dublin_bot.kraken_gateway import KrakenGateway
from dublin_bot.models import Action
from dublin_bot.ratelimit import KrakenRateLimiter, RateLimitTier
from dublin_bot.risk import SessionState
from dublin_bot.state import StateStore
from .conftest import ohlc_payload, ticker_payload, time_payload


def build_engine(settings, fake_session):
    """Engine wired to the fake session with a virtual clock.

    The rate limiter's sleeps are stubbed against a fake clock so the gate
    pipeline is exercised without spending real wall-time on token refills.
    """
    clock = {"t": 0.0}

    def sleep(seconds):
        clock["t"] += seconds

    audit = AuditLog(settings.audit_log_path)
    limiter = KrakenRateLimiter(
        RateLimitTier.pro(), time_fn=lambda: clock["t"], sleep_fn=sleep
    )
    gateway = KrakenGateway(
        settings, session=fake_session, rate_limiter=limiter,
        audit=audit, sleep_fn=lambda _s: None,
    )
    return TradingEngine(settings, gateway=gateway, audit=audit)


# ── gate ordering and blocking ──────────────────────────────────────

def test_cycle_completes_under_safe_defaults(settings, fake_session):
    result = build_engine(settings, fake_session).run_cycle()
    assert result.blocked_at is None
    assert result.record is not None
    assert "safety" in result.gates
    assert result.gates["safety"]["safety_locked"] is True


def test_cash_equity_change_is_not_realized_trading_loss(settings_factory, fake_session):
    """Withdrawals/deposits must not trip the daily trading-loss breaker."""
    settings = settings_factory()
    fake_session.routes["Balance"] = {"error": [], "result": {"ZUSD": "100.0000"}}
    fake_session.routes["TradeBalance"] = {"error": [], "result": {"eb": "100.0000", "tb": "100.0000"}}
    fake_session.routes["TradesHistory"] = {"error": [], "result": {"trades": {}}}
    store = StateStore(settings.session_state_path)
    store.save(SessionState(
        start_equity=110.0,
        peak_equity=110.0,
        current_equity=100.0,
        realized_pnl_today=0.0,
    ))

    build_engine(settings, fake_session).run_cycle()

    assert store.load(100.0).realized_pnl_today == 0.0


def test_stale_data_blocks_before_any_order(settings, fake_session):
    """The July-bars-in-August scenario must halt the cycle."""
    fake_session.routes["OHLC"] = ohlc_payload(bars=250, end_ts=time.time() - 21 * 86400)
    result = build_engine(settings, fake_session).run_cycle()
    assert result.blocked_at == "freshness"
    assert result.executed is False
    assert fake_session.endpoint_calls("AddOrder") == []


def test_clock_skew_blocks_the_cycle(settings, fake_session):
    fake_session.routes["Time"] = time_payload(time.time() + 900)
    result = build_engine(settings, fake_session).run_cycle()
    assert result.blocked_at == "freshness"
    assert "skew" in result.block_reason.lower()


def test_market_data_failure_blocks_the_cycle(settings, fake_session):
    fake_session.routes["OHLC"] = {"error": ["EQuery:Unknown asset pair"]}
    result = build_engine(settings, fake_session).run_cycle()
    assert result.blocked_at == "market_data"
    assert result.executed is False


def test_wide_spread_blocks_entry(settings_factory, fake_session):
    """A blown-out spread must veto an entry even if the strategy is bullish."""
    settings = settings_factory(max_spread_bps=1.0)
    fake_session.routes["Ticker"] = ticker_payload(bid=40_000.0, ask=60_000.0)
    result = build_engine(settings, fake_session).run_cycle()
    assert result.gates["market_quality"]["approved"] is False
    assert result.executed is False


def test_thin_volume_blocks_entry(settings_factory, fake_session):
    settings = settings_factory(min_dollar_volume=10**12)
    result = build_engine(settings, fake_session).run_cycle()
    assert result.gates["market_quality"]["approved"] is False
    assert result.executed is False


def test_inconsistent_safety_config_halts_immediately(settings_factory, fake_session):
    """Live allowed without the acknowledgement must never reach market data."""
    settings = settings_factory(
        paper_trading=True, dry_run=True,
        allow_live_trading=True,
        live_risk_acknowledgement="I_ACCEPT_LIVE_TRADING_RISK",
    )
    engine = build_engine(settings, fake_session)
    # Now corrupt the acknowledgement post-validation to simulate tampering.
    object.__setattr__(engine.settings, "live_risk_acknowledgement", "nope")
    with pytest.raises(SafetyLockError):
        engine.assert_safety_locks()


def test_blocked_cycle_still_journals_a_halt(settings, fake_session):
    fake_session.routes["OHLC"] = ohlc_payload(bars=250, end_ts=time.time() - 30 * 86400)
    engine = build_engine(settings, fake_session)
    record = engine.run_once()
    assert record.signal.action is Action.HALT
    assert record.order_id is None


# ── no live orders under locks ──────────────────────────────────────

def test_no_add_order_call_is_ever_made_in_paper_mode(settings, fake_session):
    engine = build_engine(settings, fake_session)
    for _ in range(3):
        engine.run_cycle()
    assert fake_session.endpoint_calls("AddOrder") == []


def test_gateway_reports_submission_disabled(settings, fake_session):
    engine = build_engine(settings, fake_session)
    assert engine.gateway.order_submission_enabled is False


# ── idempotency in the pipeline ─────────────────────────────────────

def test_repeated_cycles_on_the_same_bar_do_not_duplicate(settings, fake_session):
    """Two evaluations of one closed bar are one intent, not two orders."""
    frozen = ohlc_payload(bars=250)
    fake_session.routes["OHLC"] = frozen
    engine = build_engine(settings, fake_session)

    first = engine.run_cycle()
    second = engine.run_cycle()

    if first.executed:
        # Same bar, same intent → the second attempt must be blocked. Once the
        # first fill is reflected in aggregate bot-owned exposure, the risk
        # gate may stop it before the idempotency gate is reached.
        assert second.executed is False
        duplicate_blocked = second.gates.get("idempotency", {}).get("blocked") is True
        exposure_blocked = (
            second.record is not None
            and "exposure" in second.record.risk.reason.lower()
        )
        position_held = (
            second.record is not None
            and second.record.signal.action is Action.WAIT
            and second.record.risk.reason == "No entry order requested"
        )
        assert duplicate_blocked or exposure_blocked or position_held, {
            "gates": second.gates,
            "risk": repr(second.record.risk) if second.record else None,
            "blocked_at": second.blocked_at,
            "block_reason": second.block_reason,
        }
    # Regardless of signal, at most one confirmed order exists for this bar.
    confirmed = [r for r in engine.ledger._records.values() if r.status == "confirmed"]
    assert len(confirmed) <= 1


def test_ledger_persists_across_engine_restart(settings, fake_session):
    fake_session.routes["OHLC"] = ohlc_payload(bars=250)
    engine = build_engine(settings, fake_session)
    engine.run_cycle()
    keys_before = set(engine.ledger._records)

    restarted = build_engine(settings, fake_session)
    assert set(restarted.ledger._records) == keys_before


# ── restart recovery ────────────────────────────────────────────────

def test_recovery_confirms_an_order_that_actually_landed(settings, fake_session):
    """The ambiguous-submission case resolves by querying the exchange."""
    engine = build_engine(settings, fake_session)
    record = engine.ledger.reserve(
        key="orphan", symbol="BTC/USD", side="buy", notional_usd=6.25,
        bar_timestamp="2026-08-04T12:00:00Z", dry_run=False,
    )
    fake_session.routes["OpenOrders"] = {
        "error": [], "result": {"open": {"OFOUND-1": {
            "status": "open", "vol": "0.001", "vol_exec": "0", "cost": "50",
            "userref": record.userref,
            "descr": {"pair": "XBTUSD", "type": "buy", "ordertype": "market"},
        }}},
    }
    summary = engine.recover()
    assert summary["pending_found"] == 1
    assert engine.ledger.get("orphan").status == "confirmed"
    assert engine.ledger.get("orphan").order_id == "OFOUND-1"


def test_recovery_marks_absent_order_as_failed(settings, fake_session):
    engine = build_engine(settings, fake_session)
    engine.ledger.reserve(
        key="ghost", symbol="BTC/USD", side="buy", notional_usd=6.25,
        bar_timestamp="2026-08-04T12:00:00Z", dry_run=False,
    )
    engine.recover()
    assert engine.ledger.get("ghost").status == "failed"


def test_recovery_runs_before_new_intents(settings, fake_session):
    engine = build_engine(settings, fake_session)
    engine.ledger.reserve(
        key="stale-intent", symbol="BTC/USD", side="buy", notional_usd=6.25,
        bar_timestamp="2026-08-04T12:00:00Z", dry_run=False,
    )
    result = engine.run_cycle()
    assert result.gates["recovery"]["pending_found"] == 1
    assert engine.ledger.get("stale-intent").status != "pending"


# ── audit trail ─────────────────────────────────────────────────────

def test_cycle_writes_an_intact_audit_chain(settings, fake_session):
    engine = build_engine(settings, fake_session)
    engine.run_cycle()
    intact, reason = AuditLog(settings.audit_log_path).verify_chain()
    assert intact is True, reason


def test_audit_records_the_expected_events(settings, fake_session):
    engine = build_engine(settings, fake_session)
    engine.run_cycle()
    events = {e["event"] for e in AuditLog(settings.audit_log_path).tail(100)}
    assert "safety_check" in events
    assert "data_freshness" in events
    assert "signal" in events
    assert "risk_decision" in events


def test_audit_log_contains_no_credentials(settings, fake_session):
    engine = build_engine(settings, fake_session)
    engine.run_cycle()
    text = settings.audit_log_path.read_text()
    assert settings.kraken_api_secret not in text
    assert settings.kraken_api_key not in text

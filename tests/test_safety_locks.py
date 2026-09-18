"""Safety-lock regression tests.

This file exists to make an accidental relaxation of the trading locks fail
loudly in CI.  It asserts the *declared* safe state of the repository and the
invariant that no code path reachable in the default configuration can submit
an order to an exchange.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dublin_bot.config import Settings
from dublin_bot.engine import TradingEngine
from dublin_bot.errors import SafetyLockError
from dublin_bot.kraken_gateway import KrakenGateway

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── declared defaults ───────────────────────────────────────────────

def test_code_defaults_are_locked():
    s = Settings(_env_file=None)
    assert s.paper_trading is True
    assert s.dry_run is True
    assert s.allow_live_trading is False
    assert s.safety_locked is True


def test_env_example_ships_locked():
    """A copied .env.example must never produce a live-capable configuration."""
    text = (REPO_ROOT / ".env.example").read_text()
    assert "PAPER_TRADING=true" in text
    assert "DRY_RUN=true" in text
    assert "ALLOW_LIVE_TRADING=false" in text


def test_live_mode_requires_acknowledgement():
    with pytest.raises(ValueError, match="acknowledgement"):
        Settings(_env_file=None, paper_trading=False, allow_live_trading=True,
                 live_risk_acknowledgement="")


def test_live_mode_requires_allow_flag():
    with pytest.raises(ValueError, match="ALLOW_LIVE_TRADING"):
        Settings(_env_file=None, paper_trading=False, allow_live_trading=False)


def test_wrong_acknowledgement_string_is_rejected():
    with pytest.raises(ValueError):
        Settings(_env_file=None, paper_trading=False, allow_live_trading=True,
                 live_risk_acknowledgement="i accept live trading risk")


# ── risk parameter ceilings ─────────────────────────────────────────

@pytest.mark.parametrize("field,value", [
    ("risk_per_trade", 0.05),          # cap 0.02
    ("max_position_fraction", 0.9),    # cap 0.5
    ("max_daily_loss_fraction", 0.5),  # cap 0.05
    ("max_drawdown_fraction", 0.9),    # cap 0.20
    ("max_orders_per_day", 100),       # cap 10
])
def test_risk_ceilings_cannot_be_exceeded(field, value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{field: value})


def test_equity_floor_is_enforced():
    with pytest.raises(ValueError):
        Settings(_env_file=None, strategy_equity_usd=1.0)


# ── execution is unreachable under locks ────────────────────────────

def test_gateway_cannot_submit_under_default_settings(settings, fake_session):
    gw = KrakenGateway(settings, session=fake_session, allow_order_submission=True,
                       sleep_fn=lambda _s: None)
    assert gw.order_submission_enabled is False
    with pytest.raises(SafetyLockError):
        gw._assert_can_submit()


def test_full_cycle_never_calls_add_order(settings, fake_session):
    from dublin_bot.audit import AuditLog
    from dublin_bot.ratelimit import KrakenRateLimiter, RateLimitTier

    # Virtual clock so the limiter's real refill delays don't slow the suite.
    clock = {"t": 0.0}

    def sleep(seconds):
        clock["t"] += seconds

    limiter = KrakenRateLimiter(RateLimitTier.pro(),
                                time_fn=lambda: clock["t"], sleep_fn=sleep)
    gw = KrakenGateway(settings, session=fake_session, rate_limiter=limiter,
                       sleep_fn=lambda _s: None)
    engine = TradingEngine(settings, gateway=gw,
                           audit=AuditLog(settings.audit_log_path))
    for _ in range(5):
        engine.run_cycle()
    assert fake_session.endpoint_calls("AddOrder") == []
    assert fake_session.endpoint_calls("CancelOrder") == []


def test_no_withdrawal_or_transfer_endpoint_is_referenced():
    """Withdrawal endpoints must not exist anywhere in the source tree."""
    forbidden = ("Withdraw", "WithdrawFunds", "WalletTransfer",
                 "WithdrawCancel", "WithdrawStatus")
    src = REPO_ROOT / "src"
    for path in src.rglob("*.py"):
        text = path.read_text()
        for token in forbidden:
            assert token not in text, f"{path.name} references {token}"


def test_dashboard_allows_serve_when_live_mode_is_explicitly_enabled(settings_factory):
    from dublin_bot.dashboard import serve_dashboard
    live = settings_factory(
        paper_trading=False, dry_run=False, allow_live_trading=True,
        live_risk_acknowledgement="I_ACCEPT_LIVE_TRADING_RISK",
    )
    # In explicit live mode, dashboard startup should be allowed;
    # run=False avoids binding a real socket during tests.
    assert serve_dashboard(live, run=False) == 0

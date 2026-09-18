from datetime import datetime, timezone

from dublin_bot.config import Settings
from dublin_bot.models import Action, Signal
from dublin_bot.risk import RiskManager, SessionState


def test_risk_manager_caps_order_to_quarter_of_budget():
    settings = Settings(_env_file=None)
    manager = RiskManager(settings)
    signal = Signal(Action.BUY, 100, "test", price=100.0, atr=2.0, stop_price=98.0)
    state = SessionState(start_equity=25.0, peak_equity=25.0, current_equity=25.0)
    decision = manager.evaluate(signal, state)
    assert decision.approved is True
    assert decision.notional_usd <= 10.0  # 25.0 * max_position_fraction(0.40)
    assert decision.planned_loss_usd <= 0.25


def test_daily_loss_breaker_blocks_entry():
    settings = Settings(_env_file=None)
    manager = RiskManager(settings)
    signal = Signal(Action.BUY, 100, "test", price=100.0, atr=2.0, stop_price=98.0)
    state = SessionState(
        start_equity=25.0,
        peak_equity=25.0,
        current_equity=24.0,
        realized_pnl_today=-0.75,
    )
    decision = manager.evaluate(signal, state)
    assert decision.approved is False
    assert "Daily loss" in decision.reason


def test_sell_exit_approved_while_buy_blocked_by_cooldown():
    """A SELL exit must NOT be blocked by the entry cooldown."""
    settings = Settings(_env_file=None)
    manager = RiskManager(settings)
    sell = Signal(Action.SELL, 90, "Price closed below slow trend EMA", 100.0, 2.0)
    buy = Signal(Action.BUY, 100, "test", price=100.0, atr=2.0, stop_price=98.0)
    # Fresh order just happened → cooldown active now.
    state = SessionState(
        start_equity=25.0, peak_equity=25.0, current_equity=25.0,
        orders_today=0,
        last_order_at=datetime.now(timezone.utc),
    )
    blocked_buy = manager.evaluate(buy, state)
    approved_sell = manager.evaluate(sell, state)
    assert blocked_buy.approved is False
    assert "cooldown" in blocked_buy.reason.lower()
    assert approved_sell.approved is True
    assert "Exit signal approved" in approved_sell.reason


def test_sell_exit_approved_while_buy_blocked_by_order_cap():
    """A SELL exit must NOT be blocked by the daily max-orders cap.

    When a positive cap is configured, a BUY at the cap is blocked but a SELL
    exit always passes. (max_orders_per_day=0 means unlimited — covered below.)
    """
    settings = Settings(_env_file=None, max_orders_per_day=3)
    manager = RiskManager(settings)
    sell = Signal(Action.SELL, 90, "Price closed below slow trend EMA", 100.0, 2.0)
    buy = Signal(Action.BUY, 100, "test", price=100.0, atr=2.0, stop_price=98.0)
    # Daily order cap already reached.
    state = SessionState(
        start_equity=25.0, peak_equity=25.0, current_equity=25.0,
        orders_today=settings.max_orders_per_day,
        last_order_at=None,
    )
    blocked_buy = manager.evaluate(buy, state)
    approved_sell = manager.evaluate(sell, state)
    assert blocked_buy.approved is False
    assert "order limit" in blocked_buy.reason.lower()
    assert approved_sell.approved is True
    assert "Exit signal approved" in approved_sell.reason


def test_unlimited_orders_when_cap_is_zero():
    """max_orders_per_day=0 means no daily cap — buys are not blocked by count."""
    settings = Settings(_env_file=None, max_orders_per_day=0)
    manager = RiskManager(settings)
    buy = Signal(Action.BUY, 100, "test", price=100.0, atr=2.0, stop_price=98.0)
    state = SessionState(
        start_equity=25.0, peak_equity=25.0, current_equity=25.0,
        orders_today=999, last_order_at=None,
    )
    decision = manager.evaluate(buy, state)
    assert decision.approved is True
    assert "order limit" not in decision.reason.lower()


def test_sell_exit_approved_while_buy_blocked_by_daily_loss_and_drawdown():
    """A SELL exit must NOT be blocked by daily-loss or drawdown breakers."""
    settings = Settings(_env_file=None)
    manager = RiskManager(settings)
    sell = Signal(Action.SELL, 90, "Price closed below slow trend EMA", 100.0, 2.0)
    buy = Signal(Action.BUY, 100, "test", price=100.0, atr=2.0, stop_price=98.0)
    state = SessionState(
        start_equity=25.0, peak_equity=100.0, current_equity=20.0,
        realized_pnl_today=-5.0,  # > 3% daily-loss and > 10% drawdown
        orders_today=0, last_order_at=None,
    )
    blocked_buy = manager.evaluate(buy, state)
    approved_sell = manager.evaluate(sell, state)
    assert blocked_buy.approved is False
    assert approved_sell.approved is True
    assert "Exit signal approved" in approved_sell.reason

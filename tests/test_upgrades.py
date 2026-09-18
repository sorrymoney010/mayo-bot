from datetime import datetime, timezone
from pathlib import Path

from dublin_bot.market_quality import MarketGuard, MarketQuality
from dublin_bot.protective import ProtectiveStop, StopMonitor
from dublin_bot.reconciliation import OrderSnapshot, PositionSnapshot, reconcile
from dublin_bot.risk import SessionState
from dublin_bot.state import StateStore


def test_state_survives_restart(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")
    state = SessionState(25.0, 25.5, 25.2, 0.2, 2, datetime.now(timezone.utc))
    store.save(state)
    restored = store.load(25.2)
    assert restored.orders_today == 2
    assert restored.peak_equity == 25.5
    assert restored.last_order_at is not None


def test_market_guard_rejects_wide_spread_and_thin_volume():
    guard = MarketGuard(max_spread_bps=50, min_dollar_volume=100_000)
    approved, _ = guard.approve(MarketQuality(1.00, 1.02, 50_000))
    assert approved is False


def test_independent_stop_monitor():
    stop = ProtectiveStop("BTC/USD", stop_price=90.0, quantity=0.01)
    assert StopMonitor.should_exit(89.0, stop) is True
    assert StopMonitor.should_exit(91.0, stop) is False


def test_reconciliation_flags_unmanaged_assets():
    report = reconcile(
        [PositionSnapshot("DOGE/USD", 10, 2, 0.2)],
        [OrderSnapshot("1", "BTC/USD", "buy", "open")],
        {"BTC/USD"},
    )
    assert report.orphaned_symbols == ("DOGEUSD",)


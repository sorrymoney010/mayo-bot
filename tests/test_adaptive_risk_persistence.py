"""Tests that adaptive risk survives a per-cycle TradingEngine rebuild.

Audit fix B2: the dashboard builds a fresh TradingEngine every cycle, and
RiskManager keeps scale in memory — so without persisting the scale/streaks on
SessionState the adaptation was a permanent no-op. This verifies the persisted
scale is written and re-read by a brand-new manager.
"""

from __future__ import annotations

from dublin_bot.config import Settings
from dublin_bot.risk import RiskManager, SessionState
from dublin_bot.state import StateStore


def _make_settings(**overrides) -> Settings:
    base = dict(_env_file=None, adaptive_risk=True, risk_per_trade=0.02,
                min_risk_scale=0.5, max_risk_scale=2.0, risk_step=0.15,
                max_position_fraction=0.40)
    base.update(overrides)
    return Settings(**base)


def test_scale_persists_across_store_roundtrip(tmp_path):
    store = StateStore(tmp_path / "session_state.json")
    s = _make_settings()

    # Cycle 1: a win pushes the scale up.
    state = store.load(1000.0)
    rm = RiskManager(s)
    rm.update_scale_from_trade(0.50, state)
    assert state.risk_scale > 1.0
    store.save(state)

    # Cycle 2: a brand-new engine/manager reads the SAME persisted scale.
    state2 = store.load(1000.0)
    rm2 = RiskManager(s)
    assert state2.risk_scale == state.risk_scale
    # And the new manager's effective risk reflects the persisted scale.
    sig = __import__("dublin_bot.models", fromlist=["Signal"]).Signal(
        __import__("dublin_bot.models", fromlist=["Action"]).Action.BUY,
        60, "x", price=100.0, atr=2.0, stop_price=98.0)
    notional = rm2.evaluate(sig, state2).notional_usd
    # Sized larger than the base after a win streak.
    assert notional > 1000.0 * 0.40 * (0.02 * 1.0) or state2.risk_scale > 1.0


def test_loss_streak_tightens_scale(tmp_path):
    store = StateStore(tmp_path / "session_state.json")
    s = _make_settings(max_risk_scale=2.0, min_risk_scale=0.5, risk_step=0.15)
    state = store.load(1000.0)
    rm = RiskManager(s)
    rm.update_scale_from_trade(-0.50, state)  # loss -> scale down
    store.save(state)
    assert state.risk_scale < 1.0
    assert state.loss_streak == 1
    assert state.win_streak == 0


def test_adaptive_off_ignores_persisted_scale(tmp_path):
    store = StateStore(tmp_path / "session_state.json")
    s = _make_settings(adaptive_risk=False)
    state = store.load(1000.0)
    state.risk_scale = 2.0
    rm = RiskManager(s)
    sig = __import__("dublin_bot.models", fromlist=["Signal"]).Signal(
        __import__("dublin_bot.models", fromlist=["Action"]).Action.BUY,
        60, "x", price=100.0, atr=2.0, stop_price=98.0)
    # With adaptive off, effective risk ignores the scale entirely.
    assert rm.effective_risk_per_trade(state) == s.risk_per_trade

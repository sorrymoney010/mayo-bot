"""Tests for defined-risk momentum breakout sleeve (signal, sizing, gates)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from dublin_bot.config import Settings
from dublin_bot.indicators import fee_edge_ok
from dublin_bot.models import Action
from dublin_bot.risk import RiskManager, SessionState
from dublin_bot.strategies.breakout_strategy import (
    BreakoutParams,
    BreakoutStrategy,
    detect_breakout,
    size_breakout_quantity,
)
from dublin_bot.strategy import build_strategy


def _settings(**kwargs) -> Settings:
    base = dict(
        _env_file=None,
        paper_trading=True,
        dry_run=True,
        allow_live_trading=False,
        strategy="breakout",
        breakout_lookback=20,
        breakout_vol_avg=20,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        risk_per_trade=0.01,
        max_position_fraction=0.40,
        strategy_equity_usd=100.0,
        min_edge_bps=100.0,
        min_edge_gate_enabled=True,
        adx_gate_enabled=True,
        adaptive_risk=False,
    )
    base.update(kwargs)
    return Settings(**base)


def _bars(
    closes: np.ndarray,
    *,
    vol: float | np.ndarray = 1000.0,
    high_boost: float = 0.0,
) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    if np.isscalar(vol):
        volumes = np.full(n, float(vol))
    else:
        volumes = np.asarray(vol, dtype=float)
    idx = pd.date_range("2026-01-01", periods=n, freq="15min")
    high = closes * (1.0 + 0.001) + high_boost
    low = closes * (1.0 - 0.001)
    return pd.DataFrame(
        {"open": closes, "high": high, "low": low, "close": closes, "volume": volumes},
        index=idx,
    )


def test_factory_wires_breakout_aliases():
    for name in ("breakout", "momentum_breakout"):
        s = _settings(strategy=name)
        strat = build_strategy(s)
        assert isinstance(strat, BreakoutStrategy)


def test_detect_breakout_fires_on_high_and_volume():
    # Flat range then a close above prior highs with elevated volume.
    n = 30
    closes = np.full(n, 100.0)
    closes[-1] = 105.0  # breakout close
    highs_extra = np.zeros(n)
    # Prior bars: high capped near 101 (close*1.001 + 0)
    vols = np.full(n, 1000.0)
    vols[-1] = 2500.0  # > avg
    bars = _bars(closes, vol=vols)
    # Ensure prior highs stay below 105: default high = close*1.001 = 100.1
    fired, reason, price, lookback_high, vr = detect_breakout(
        bars, BreakoutParams(lookback_bars=20, volume_avg_bars=20)
    )
    assert fired is True
    assert price == pytest.approx(105.0)
    assert lookback_high < 105.0
    assert vr > 1.0
    assert "Breakout" in reason


def test_detect_breakout_rejects_without_volume():
    n = 30
    closes = np.full(n, 100.0)
    closes[-1] = 105.0
    vols = np.full(n, 1000.0)
    vols[-1] = 500.0  # below avg
    bars = _bars(closes, vol=vols)
    fired, reason, *_ = detect_breakout(
        bars, BreakoutParams(lookback_bars=20, volume_avg_bars=20)
    )
    assert fired is False
    assert "vol" in reason.lower() or "No breakout" in reason


def test_detect_breakout_rejects_without_high_break():
    n = 30
    closes = np.linspace(100.0, 102.0, n)  # grind up; last close may not clear prior high
    # Force last close equal to prior high by flattening
    closes = np.full(n, 100.0)
    closes[-1] = 100.05  # still below high of prior (100.1)
    vols = np.full(n, 1000.0)
    vols[-1] = 3000.0
    bars = _bars(closes, vol=vols)
    fired, reason, *_ = detect_breakout(
        bars, BreakoutParams(lookback_bars=20, volume_avg_bars=20)
    )
    assert fired is False
    assert "No breakout" in reason


def test_strategy_buy_sets_2pct_stop():
    s = _settings()
    strat = BreakoutStrategy(s)
    n = 30
    closes = np.full(n, 100.0)
    closes[-1] = 105.0
    vols = np.full(n, 1000.0)
    vols[-1] = 2500.0
    bars = _bars(closes, vol=vols)
    sig = strat.evaluate(bars, in_position=False)
    assert sig.action is Action.BUY
    assert sig.stop_price == pytest.approx(105.0 * 0.98, rel=1e-9)
    assert sig.score >= 70


def test_strategy_hold_when_in_position():
    s = _settings()
    strat = BreakoutStrategy(s)
    n = 30
    closes = np.full(n, 100.0)
    closes[-1] = 105.0
    vols = np.full(n, 1000.0)
    vols[-1] = 2500.0
    bars = _bars(closes, vol=vols)
    sig = strat.evaluate(bars, in_position=True)
    assert sig.action is Action.WAIT
    assert "Holding" in sig.reason


def test_size_breakout_quantity_1pct_at_2pct_stop():
    # risk $1 on $100 equity at 2% stop on $100 entry → 0.5 units
    qty = size_breakout_quantity(
        equity=100.0, risk_pct=0.01, entry_price=100.0, stop_price=98.0
    )
    assert qty == pytest.approx(0.5)


def test_risk_manager_sizes_near_1pct_equity():
    s = _settings(risk_per_trade=0.01, stop_loss_pct=0.02, strategy_equity_usd=100.0)
    rm = RiskManager(s)
    state = SessionState(start_equity=100.0, peak_equity=100.0, current_equity=100.0)
    from dublin_bot.models import Signal

    sig = Signal(Action.BUY, 80, "breakout", 100.0, 1.0, stop_price=98.0)
    decision = rm.evaluate(sig, state)
    assert decision.approved
    # notional ≈ risk / stop_frac = 1 / 0.02 = 50, capped by max_position_fraction 0.4 → 40
    assert decision.notional_usd == pytest.approx(40.0, rel=0.01)
    # planned loss = notional * stop_frac = 40 * 0.02 = 0.8 (cap binds before full 1%)
    assert decision.planned_loss_usd == pytest.approx(0.8, rel=0.05)


def test_fee_gate_clears_at_4pct_tp():
    ok, reason = fee_edge_ok(0.04, 100.0)
    assert ok is True
    assert "Fee edge ok" in reason


def test_fee_gate_blocks_micro_tp():
    ok, reason = fee_edge_ok(0.005, 100.0)  # 0.5% < 1%
    assert ok is False
    assert "Fee gate" in reason


def test_engine_adx_not_applied_to_breakout(monkeypatch):
    """ADX sit-out is momentum-only; breakout sleeve must pass through."""
    from dublin_bot.engine import TradingEngine
    from dublin_bot.models import Signal

    s = _settings(strategy="breakout", adx_gate_enabled=True, take_profit_pct=0.04)
    # Minimal engine without hitting network: construct then call gate helper.
    eng = object.__new__(TradingEngine)
    eng.settings = s
    eng.audit = type("A", (), {"record": staticmethod(lambda *a, **k: None)})()
    bars = _bars(np.full(40, 100.0))
    buy = Signal(Action.BUY, 80, "breakout", 100.0, 1.0, stop_price=98.0)
    out = TradingEngine._apply_adx_and_fee_gates(eng, buy, bars)
    assert out.action is Action.BUY
    assert out.reason == "breakout"


def test_stop_pct_alias_from_env(monkeypatch):
    monkeypatch.setenv("STOP_PCT", "0.02")
    monkeypatch.setenv("RISK_PCT", "0.01")
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("ALLOW_LIVE_TRADING", "false")
    s = Settings(_env_file=None)
    assert s.stop_loss_pct == pytest.approx(0.02)
    assert s.risk_per_trade == pytest.approx(0.01)

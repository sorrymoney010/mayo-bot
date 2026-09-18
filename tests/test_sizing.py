"""Tests for the position-sizing engine (vol-target + fractional Kelly)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from dublin_bot.sizing import EdgeEstimate, PositionSizer, SizingConfig


def _returns(annual_vol: float, n: int = 60, seed: int = 7) -> pd.Series:
    rng = np.random.default_rng(seed)
    daily = annual_vol / np.sqrt(365)
    return pd.Series(rng.normal(0.0, daily, n))


def test_realized_vol_blend_and_floor():
    sizer = PositionSizer(SizingConfig(min_vol=0.05))
    # Calm market -> vol below floor is lifted to the floor.
    calm = _returns(0.02)
    assert sizer.realized_vol(calm) >= 0.05 - 1e-9
    # Active market returns a positive blended vol.
    active = _returns(0.40)
    assert sizer.realized_vol(active) > 0.05


def test_realized_vol_too_short_returns_floor():
    sizer = PositionSizer(SizingConfig(min_vol=0.07))
    assert sizer.realized_vol(pd.Series([0.01, 0.02])) == 0.07


def test_kelly_bounds():
    sizer = PositionSizer(SizingConfig(kelly_fraction=0.25))
    # Positive edge -> fractional Kelly, capped at 0.25.
    k = sizer.kelly_fractional(win_prob=0.55, avg_win=1.8, avg_loss=1.0, confidence=1.0)
    assert 0.0 < k <= 0.25
    # Degenerate inputs -> zero (never negative or full Kelly).
    assert sizer.kelly_fractional(0.5, 1.0, 0.0) == 0.0
    assert sizer.kelly_fractional(1.0, 1.0, 1.0) == 0.0


def test_size_respects_risk_per_trade_gate():
    sizer = PositionSizer(SizingConfig(max_risk_per_trade=0.01, max_leverage=2.0))
    rets = _returns(0.20)
    # stop_distance $0.015 on $8.58 equity -> risk gate caps shares hard.
    res = sizer.size(equity=8.58, current_price=1.0, returns=rets, stop_distance=0.015)
    assert res.notional > 0
    max_risk = 8.58 * 0.01
    assert res.notional * 0.015 <= max_risk + 1e-9


def test_size_leverage_capped():
    sizer = PositionSizer(SizingConfig(max_leverage=2.0, target_vol=0.5))
    rets = _returns(0.05)  # very low vol -> would want huge size
    res = sizer.size(equity=100.0, current_price=1.0, returns=rets, stop_distance=0.05)
    assert res.leverage <= 2.0 + 1e-9
    assert res.notional <= 200.0 + 1e-9


def test_edge_estimate_to_dict():
    e = EdgeEstimate(win_prob=0.6, avg_win=2.0, avg_loss=1.0, confidence=0.9)
    assert e.to_dict() == {"p": 0.6, "avg_win": 2.0, "avg_loss": 1.0, "conf": 0.9}

"""E1: ATR / volatility-scaled stop helper + clamp behaviour."""

from __future__ import annotations

import pytest

from dublin_bot.orders import (
    ATR_STOP_MAX_PCT_FACTOR,
    ATR_STOP_MIN_PCT_FACTOR,
    atr_scaled_stop,
)


def test_atr_path_long():
    stop, src = atr_scaled_stop(
        "buy", 100.0, atr=2.0, atr_multiplier=1.5, stop_loss_pct=0.04
    )
    assert src == "atr"
    assert stop == pytest.approx(97.0)  # 100 - 2*1.5


def test_atr_path_short():
    stop, src = atr_scaled_stop(
        "sell", 100.0, atr=2.0, atr_multiplier=1.5, stop_loss_pct=0.04
    )
    assert src == "atr"
    assert stop == pytest.approx(103.0)


def test_pct_fallback_when_atr_missing():
    stop, src = atr_scaled_stop("buy", 100.0, atr=None, stop_loss_pct=0.04)
    assert src == "pct"
    assert stop == pytest.approx(96.0)


def test_pct_fallback_when_atr_non_positive():
    stop, src = atr_scaled_stop("buy", 100.0, atr=0.0, stop_loss_pct=0.05)
    assert src == "pct"
    assert stop == pytest.approx(95.0)


def test_clamp_floor_prevents_overly_tight_atr():
    # Raw ATR distance 0.15 (0.15%) would be tighter than 50% of 4% = 2%.
    stop, src = atr_scaled_stop(
        "buy", 100.0, atr=0.1, atr_multiplier=1.5, stop_loss_pct=0.04
    )
    assert src == "atr"
    assert stop == pytest.approx(100.0 * (1.0 - 0.04 * ATR_STOP_MIN_PCT_FACTOR))


def test_clamp_ceiling_prevents_overly_wide_atr():
    # Raw ATR distance 30 would exceed 3x of 4% = 12%.
    stop, src = atr_scaled_stop(
        "buy", 100.0, atr=20.0, atr_multiplier=1.5, stop_loss_pct=0.04
    )
    assert src == "atr"
    assert stop == pytest.approx(100.0 * (1.0 - 0.04 * ATR_STOP_MAX_PCT_FACTOR))

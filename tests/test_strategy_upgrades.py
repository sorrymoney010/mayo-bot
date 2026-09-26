"""Tests for trend-gate, S/R flip, and pattern strategy upgrades."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from dublin_bot.config import Settings
from dublin_bot.indicators import enrich
from dublin_bot.models import Action
from dublin_bot.strategies.pattern_strategy import PatternStrategy
from dublin_bot.strategies.rotation_strategy import RotationStrategy
from dublin_bot.strategies.sr_flip_strategy import SRFlipStrategy
from dublin_bot.strategy import TrendBreakoutStrategy, build_strategy


def _settings(**kwargs) -> Settings:
    base = dict(
        _env_file=None,
        paper_trading=True,
        dry_run=True,
        allow_live_trading=False,
        fast_ema=5,
        slow_ema=10,
        regime_ema=20,
        rsi_min=35.0,
        rsi_max=75.0,
        atr_period=5,
        breakout_lookback=10,
        volume_lookback=10,
    )
    base.update(kwargs)
    return Settings(**base)


def _ohlc(close: np.ndarray, vol: float = 1000.0) -> pd.DataFrame:
    close = np.asarray(close, dtype=float)
    n = len(close)
    idx = pd.date_range("2026-01-01", periods=n, freq="15min")
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.002,
            "low": close * 0.998,
            "close": close,
            "volume": np.full(n, vol),
        },
        index=idx,
    )


def _momentum_below_regime_bars() -> pd.DataFrame:
    """Downtrend then mild bounce: RSI in band, close still below regime EMA."""
    # Search a bounce size that lands RSI in [35,75] while close <= ema_regime.
    n = 100
    base = np.linspace(130.0, 80.0, n)
    from dublin_bot.indicators import enrich as _enrich
    chosen = None
    for bump in np.linspace(1.0, 8.0, 40):
        close = base.copy()
        close[-10:] = np.linspace(close[-10], close[-10] + bump, 10)
        bars = _ohlc(close)
        frame = _enrich(
            bars, fast_ema=5, slow_ema=10, regime_ema=20, rsi_period=14,
            atr_period=5, breakout_lookback=10, volume_lookback=10,
        ).dropna()
        row = frame.iloc[-1]
        rsi = float(row["rsi"])
        if 35.0 <= rsi <= 75.0 and float(row["close"]) <= float(row["ema_regime"]):
            chosen = bars
            break
    assert chosen is not None, "could not synthesize below-regime momentum bars"
    return chosen


def _momentum_above_regime_bars() -> pd.DataFrame:
    """Uptrend with RSI in tradable band and price above regime EMA."""
    n = 80
    close = np.linspace(80.0, 120.0, n)
    # Flatten last few bars slightly so RSI isn't pegged at 100.
    close[-5:] = close[-5] + np.linspace(0, 0.8, 5)
    return _ohlc(close, vol=2000.0)


def test_momentum_blocked_below_regime():
    s = _settings(strategy="momentum")
    strat = TrendBreakoutStrategy(s)
    bars = _momentum_below_regime_bars()
    frame = enrich(
        bars,
        fast_ema=s.fast_ema,
        slow_ema=s.slow_ema,
        regime_ema=s.regime_ema,
        rsi_period=s.rsi_period,
        atr_period=s.atr_period,
        breakout_lookback=s.breakout_lookback,
        volume_lookback=s.volume_lookback,
    ).dropna()
    row = frame.iloc[-1]
    assert s.rsi_min <= float(row["rsi"]) <= s.rsi_max, float(row["rsi"])
    assert float(row["close"]) <= float(row["ema_regime"])

    sig = strat.evaluate(bars, in_position=False)
    assert sig.action is Action.WAIT
    assert sig.reason == "Momentum blocked: counter-trend"


def test_momentum_allowed_above_regime():
    s = _settings(strategy="momentum")
    strat = TrendBreakoutStrategy(s)
    bars = _momentum_above_regime_bars()
    frame = enrich(
        bars,
        fast_ema=s.fast_ema,
        slow_ema=s.slow_ema,
        regime_ema=s.regime_ema,
        rsi_period=s.rsi_period,
        atr_period=s.atr_period,
        breakout_lookback=s.breakout_lookback,
        volume_lookback=s.volume_lookback,
    ).dropna()
    row = frame.iloc[-1]
    assert s.rsi_min <= float(row["rsi"]) <= s.rsi_max, float(row["rsi"])
    assert float(row["close"]) > float(row["ema_regime"])

    sig = strat.evaluate(bars, in_position=False)
    assert sig.action is Action.BUY
    assert "Momentum confirmed" in sig.reason
    assert "counter-trend" not in sig.reason


def test_momentum_in_position_exit_unchanged():
    s = _settings(strategy="momentum")
    strat = TrendBreakoutStrategy(s)
    # Price below slow EMA while "in position" → SELL regardless of regime.
    n = 60
    close = np.linspace(100.0, 70.0, n)
    bars = _ohlc(close)
    sig = strat.evaluate(bars, in_position=True)
    assert sig.action is Action.SELL
    assert "slow trend EMA" in sig.reason


def test_rotation_blocks_counter_trend_buy():
    # Long advance, sharp dump, partial bounce: lookback momentum still positive
    # but price remains below EMA200 (counter-trend).
    s = _settings(strategy="momentum", regime_ema=200, lookback_bars=220)
    strat = RotationStrategy(s)
    n = 260
    close = np.empty(n)
    close[:200] = np.linspace(50.0, 100.0, 200)
    close[200:210] = np.linspace(100.0, 70.0, 10)
    close[210:] = np.linspace(70.0, 74.0, 50)
    bars = _ohlc(close)
    sig = strat.evaluate(bars, in_position=False)
    assert sig.action is Action.WAIT
    assert sig.reason == "Momentum blocked: counter-trend"


def _sr_flip_fresh_break_bars() -> pd.DataFrame:
    """Pivot high around 100, then fresh break above on the last bar."""
    # Build a clear local high at index with strength=2 on each side.
    # ... 98,99,100,99,98 ... then grind sideways below 100, then break 101.
    vals = [90.0] * 20
    # Approach and form pivot high
    vals += [95, 97, 99, 100, 99, 97, 95]  # pivot at 100 with strength 2
    # Stay below resistance
    vals += [96, 97, 98, 97, 96, 97, 98, 99]
    # Prior close at/below 100, last bar breaks
    vals += [99.5, 101.5]
    close = np.array(vals, dtype=float)
    high = close.copy()
    low = close.copy()
    # Exaggerate the pivot high bar
    pivot_i = 20 + 3  # the 100 bar
    high[pivot_i] = 100.0
    for i in range(len(close)):
        if i != pivot_i:
            high[i] = close[i] * 1.001
        low[i] = close[i] * 0.999
    high[-1] = 101.8
    n = len(close)
    idx = pd.date_range("2026-01-01", periods=n, freq="15min")
    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": 1000.0},
        index=idx,
    )


def test_sr_flip_buy_on_resistance_break():
    s = _settings(
        strategy="sr_flip",
        sr_lookback=80,
        sr_pivot_strength=2,
        sr_min_break_atr=0.0,
        sr_min_break_pct=0.005,
    )
    strat = SRFlipStrategy(s)
    bars = _sr_flip_fresh_break_bars()
    sig = strat.evaluate(bars, in_position=False)
    assert sig.action is Action.BUY
    assert "S/R flip BUY" in sig.reason
    assert sig.stop_price is not None and sig.stop_price < sig.price


def test_sr_flip_waits_without_setup():
    s = _settings(strategy="sr_flip", sr_pivot_strength=2)
    strat = SRFlipStrategy(s)
    # Flat grind — no break of pivots.
    close = np.full(60, 100.0) + np.sin(np.linspace(0, 6, 60)) * 0.2
    bars = _ohlc(close)
    sig = strat.evaluate(bars, in_position=False)
    assert sig.action is Action.WAIT


def _double_bottom_break_bars() -> pd.DataFrame:
    """Two troughs near 90, neckline ~100, last bar breaks neckline."""
    # Flat, down to 90, up to 100, down to 90, up toward neckline, break.
    parts = []
    parts.append(np.linspace(100, 100, 10))
    parts.append(np.linspace(100, 90, 8))   # first bottom approach
    parts.append(np.array([90.0, 90.0, 90.5]))  # bottom 1 plateau
    parts.append(np.linspace(90.5, 100, 8))  # rally to neckline
    parts.append(np.array([100.0, 100.2, 99.8]))  # neckline area
    parts.append(np.linspace(99.8, 90, 8))  # second bottom
    parts.append(np.array([90.0, 90.1, 90.0]))  # bottom 2
    parts.append(np.linspace(90.0, 99.5, 6))  # approach neckline
    parts.append(np.array([99.0, 101.5]))  # prior below, break above
    close = np.concatenate(parts)
    high = close * 1.002
    low = close * 0.998
    # Pin exact bottoms and neckline highs for clarity
    # Find approximate indices of bottoms (~90) and neckline (~100)
    n = len(close)
    idx = pd.date_range("2026-01-01", periods=n, freq="15min")
    # Boost neckline high between bottoms
    mid = n // 2
    high[mid - 5 : mid + 5] = np.maximum(high[mid - 5 : mid + 5], 100.3)
    high[-1] = 102.0
    low[-1] = 100.5
    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": 1000.0},
        index=idx,
    )


def test_pattern_double_bottom_neckline_break():
    s = _settings(
        strategy="pattern",
        pattern_pivot_strength=2,
        pattern_dbl_tol_pct=0.03,
        pattern_min_break_atr=0.0,
        pattern_min_break_pct=0.005,
    )
    strat = PatternStrategy(s)
    bars = _double_bottom_break_bars()
    sig = strat.evaluate(bars, in_position=False)
    assert sig.action is Action.BUY, sig.reason
    assert "Double-bottom" in sig.reason
    assert "invalidate" in sig.reason.lower()
    assert sig.stop_price is not None


def _bullish_flag_bars() -> pd.DataFrame:
    """Sharp pole then tight flag, last bar breaks flag high."""
    pole = np.linspace(100.0, 110.0, 8)  # +10%
    flag = np.array([109.5, 109.2, 109.0, 109.3, 109.1, 109.4])  # tight
    breakout = np.array([109.2, 110.5])  # prior inside, break above flag high ~109.5
    close = np.concatenate([np.full(40, 100.0), pole, flag, breakout])
    high = close * 1.001
    low = close * 0.999
    # Keep flag highs capped so break is clear
    flag_start = 40 + 8
    flag_end = flag_start + 6
    high[flag_start:flag_end] = np.minimum(high[flag_start:flag_end], 109.55)
    high[-1] = 110.8
    n = len(close)
    idx = pd.date_range("2026-01-01", periods=n, freq="15min")
    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": 1000.0},
        index=idx,
    )


def test_pattern_bullish_flag_breakout():
    s = _settings(
        strategy="pattern",
        pattern_flag_pole_pct=0.05,
        pattern_flag_pole_bars=8,
        pattern_flag_bars=6,
        pattern_flag_max_range_pct=0.03,
        pattern_min_break_atr=0.0,
        pattern_min_break_pct=0.002,
        # Make double-bottom unlikely so flag path is exercised.
        pattern_dbl_tol_pct=0.001,
    )
    strat = PatternStrategy(s)
    bars = _bullish_flag_bars()
    sig = strat.evaluate(bars, in_position=False)
    assert sig.action is Action.BUY, sig.reason
    assert "Bullish flag" in sig.reason


def test_pattern_invalidation_sell():
    s = _settings(strategy="pattern")
    strat = PatternStrategy(s)
    strat._invalidation = 95.0
    strat._active_pattern = "double_bottom"
    close = np.linspace(100, 94, 50)
    bars = _ohlc(close)
    sig = strat.evaluate(bars, in_position=True)
    assert sig.action is Action.SELL
    assert "invalidation" in sig.reason.lower()


def test_build_strategy_wires_new_names():
    assert isinstance(build_strategy(_settings(strategy="sr_flip")), SRFlipStrategy)
    assert isinstance(build_strategy(_settings(strategy="pattern")), PatternStrategy)
    assert isinstance(build_strategy(_settings(strategy="elliott_lite")), PatternStrategy)
    assert isinstance(build_strategy(_settings(strategy="momentum")), TrendBreakoutStrategy)


def test_live_still_locked_defaults():
    s = Settings(_env_file=None)
    assert s.paper_trading is True
    assert s.dry_run is True
    assert s.allow_live_trading is False
    assert s.safety_locked is True

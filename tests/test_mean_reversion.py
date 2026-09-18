"""Tests for the MeanReversionStrategy (the backtested-best signal)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from dublin_bot.config import Settings
from dublin_bot.indicators import enrich
from dublin_bot.models import Action
from dublin_bot.strategy import MeanReversionStrategy, TrendBreakoutStrategy, build_strategy


def _enriched(rsi_target: float) -> pd.DataFrame:
    """Build an enriched OHLC frame whose *natural* RSI/EMA match the request.

    The strategy re-runs ``enrich`` internally, so we must shape the price
    series so the computed indicators come out as intended rather than forcing
    column values that get overwritten.
    """
    n = 120
    rng = np.random.default_rng(7)
    # Start calm, then impose a move that lands RSI where we want.
    base = np.ones(n) * 100.0
    if rsi_target <= 32:           # washed-out: steep terminal drop
        close_p = base.copy()
        close_p[-15:] -= np.linspace(0, 14, 15)   # hard crash into the last bars
        close_p += rng.normal(0, 0.05, n)
    elif rsi_target >= 55:         # recovered: rebound off a low
        dip = np.linspace(8, 0, n)
        close_p = base - dip + rng.normal(0, 0.05, n)
    else:                          # neutral
        close_p = base + rng.normal(0, 0.1, n)
    close_p = np.maximum(close_p, 1.0)
    idx = pd.date_range("2026-01-01", periods=n, freq="15min")
    df = pd.DataFrame({
        "time": np.arange(n), "open": close_p, "high": close_p * 1.01,
        "low": close_p * 0.99, "close": close_p, "volume": 1000.0,
    }, index=idx)
    df = enrich(df, fast_ema=20, slow_ema=50, regime_ema=200, rsi_period=14,
                atr_period=14, breakout_lookback=20, volume_lookback=20)
    return df.dropna()


def test_mr_buys_when_oversold_and_below_slow():
    s = Settings(strategy="mean_reversion")
    strat = MeanReversionStrategy(s)
    bars = _enriched(rsi_target=28.0)
    row = bars.iloc[-1]
    assert float(row["rsi"]) <= 32.0 and float(row["close"]) < float(row["ema_slow"])
    sig = strat.evaluate(bars, in_position=False)
    assert sig.action is Action.BUY
    assert "Oversold" in sig.reason


def test_mr_waits_when_not_oversold():
    s = Settings(strategy="mean_reversion")
    strat = MeanReversionStrategy(s)
    bars = _enriched(rsi_target=55.0)
    sig = strat.evaluate(bars, in_position=False)
    assert sig.action is Action.WAIT


def test_mr_sells_on_reversion_in_position():
    s = Settings(strategy="mean_reversion")
    strat = MeanReversionStrategy(s)
    bars = _enriched(rsi_target=60.0)
    sig = strat.evaluate(bars, in_position=True)
    assert sig.action is Action.SELL


def test_mr_holds_in_position_when_still_oversold():
    s = Settings(strategy="mean_reversion")
    strat = MeanReversionStrategy(s)
    bars = _enriched(rsi_target=28.0)
    sig = strat.evaluate(bars, in_position=True)
    assert sig.action is Action.WAIT


def test_build_strategy_factory_selects_mr():
    assert isinstance(build_strategy(Settings(strategy="mean_reversion")), MeanReversionStrategy)
    # Default strategy is now momentum (aggressive, more frequent entries);
    # mean-reversion is still selectable explicitly.
    assert isinstance(build_strategy(Settings()), TrendBreakoutStrategy)
    assert isinstance(build_strategy(Settings(strategy="momentum")), TrendBreakoutStrategy)

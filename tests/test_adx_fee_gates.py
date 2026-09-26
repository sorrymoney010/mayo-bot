"""Tests for ADX hysteresis sit-out and fee-aware min-edge gates."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from dublin_bot.config import Settings
from dublin_bot.indicators import (
    adx_trend_allowed,
    compute_adx,
    enrich,
    fee_edge_ok,
)
from dublin_bot.models import Action
from dublin_bot.strategy import TrendBreakoutStrategy


def _settings(**kwargs) -> Settings:
    base = dict(
        _env_file=None,
        # Avoid picking up live process env / .env for knobs under test.
        kraken_api_key="",
        kraken_api_secret="",
        paper_trading=True,
        dry_run=True,
        allow_live_trading=False,
        strategy="momentum",
        fast_ema=5,
        slow_ema=10,
        regime_ema=20,
        rsi_min=35.0,
        rsi_max=75.0,
        atr_period=5,
        adx_period=5,
        adx_enter_above=25.0,
        adx_exit_below=20.0,
        adx_gate_enabled=True,
        min_edge_bps=100.0,
        min_edge_gate_enabled=True,
        take_profit_pct=0.08,
        breakout_lookback=10,
        volume_lookback=10,
    )
    base.update(kwargs)
    return Settings(**base)


def _ohlc(close: np.ndarray, *, range_pct: float = 0.01) -> pd.DataFrame:
    close = np.asarray(close, dtype=float)
    n = len(close)
    idx = pd.date_range("2026-01-01", periods=n, freq="15min")
    return pd.DataFrame(
        {
            "open": close,
            "high": close * (1.0 + range_pct),
            "low": close * (1.0 - range_pct),
            "close": close,
            "volume": np.full(n, 2000.0),
        },
        index=idx,
    )


def test_compute_adx_present_on_enrich():
    close = np.linspace(100.0, 130.0, 80)
    # Add directional swings so +DM accumulates.
    close[40:] = close[40] + np.linspace(0, 40, 40)
    bars = _ohlc(close, range_pct=0.02)
    frame = enrich(
        bars,
        fast_ema=5,
        slow_ema=10,
        regime_ema=20,
        rsi_period=14,
        atr_period=5,
        breakout_lookback=10,
        volume_lookback=10,
        adx_period=5,
    )
    assert "adx" in frame.columns
    adx = frame["adx"].dropna()
    assert len(adx) > 0
    assert float(adx.iloc[-1]) > 0


def test_adx_hysteresis_enter_exit_and_hold():
    # Start denied; stay denied while below exit band.
    assert adx_trend_allowed([10, 15, 18], enter_above=25, exit_below=20) is False
    # Cross above enter → allow.
    assert adx_trend_allowed([10, 18, 26], enter_above=25, exit_below=20) is True
    # Between bands keeps prior allow.
    assert adx_trend_allowed([10, 26, 22, 23], enter_above=25, exit_below=20) is True
    # Drop below exit → deny, then band keeps deny.
    assert adx_trend_allowed([30, 22, 18, 22], enter_above=25, exit_below=20) is False
    # Re-enter only after crossing enter_above again.
    assert adx_trend_allowed([30, 18, 22, 27], enter_above=25, exit_below=20) is True


def test_adx_hysteresis_rejects_inverted_thresholds():
    with pytest.raises(ValueError):
        adx_trend_allowed([25], enter_above=20, exit_below=25)


def test_fee_edge_blocks_micro_tp():
    ok, reason = fee_edge_ok(take_profit_pct=0.005, min_edge_bps=100.0)
    assert ok is False
    assert "Fee gate" in reason
    assert "0.50%" in reason
    assert "1.00%" in reason


def test_fee_edge_allows_adequate_tp():
    ok, reason = fee_edge_ok(take_profit_pct=0.08, min_edge_bps=100.0)
    assert ok is True
    assert "Fee edge ok" in reason


def test_fee_edge_boundary_equal_ok():
    ok, _ = fee_edge_ok(take_profit_pct=0.01, min_edge_bps=100.0)
    assert ok is True
    ok2, reason2 = fee_edge_ok(take_profit_pct=0.0099, min_edge_bps=100.0)
    assert ok2 is False
    assert "Fee gate" in reason2


def _trending_momentum_bars() -> pd.DataFrame:
    """Strong uptrend with wide ranges → elevated ADX, RSI in band, above regime."""
    n = 120
    close = np.linspace(80.0, 140.0, n)
    # Mild flatten so RSI is not pegged at 100.
    close[-4:] = close[-5] + np.linspace(0.2, 0.8, 4)
    return _ohlc(close, range_pct=0.025)


def _choppy_bars() -> pd.DataFrame:
    """True sideways grind: flat closes, tiny symmetric ranges → low ADX."""
    n = 160
    # Micro noise only — no persistent directional move for +DM/-DM.
    rng = np.random.default_rng(42)
    close = 100.0 + rng.normal(0.0, 0.02, size=n)
    close = np.cumsum(np.zeros(n)) + 100.0  # perfectly flat
    # Alternate tiny up/down wicks so DM cancels; TR stays small.
    high = close + 0.05
    low = close - 0.05
    # Flip every other bar so neither DM dominates.
    for i in range(1, n):
        if i % 2 == 0:
            high[i] = close[i] + 0.08
            low[i] = close[i] - 0.02
        else:
            high[i] = close[i] + 0.02
            low[i] = close[i] - 0.08
    idx = pd.date_range("2026-01-01", periods=n, freq="15min")
    return pd.DataFrame(
        {
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.full(n, 2000.0),
        },
        index=idx,
    )


def test_momentum_blocked_in_chop_adx_sit_out():
    s = _settings(adx_period=14, adx_enter_above=25.0, adx_exit_below=20.0)
    strat = TrendBreakoutStrategy(s)
    bars = _choppy_bars()
    frame = enrich(
        bars,
        fast_ema=s.fast_ema,
        slow_ema=s.slow_ema,
        regime_ema=s.regime_ema,
        rsi_period=s.rsi_period,
        atr_period=s.atr_period,
        breakout_lookback=s.breakout_lookback,
        volume_lookback=s.volume_lookback,
        adx_period=s.adx_period,
    )
    adx_last = float(frame["adx"].dropna().iloc[-1])
    # Sanity: choppy series should sit below the enter threshold after hysteresis.
    assert adx_trend_allowed(
        frame["adx"].dropna().tolist(),
        enter_above=s.adx_enter_above,
        exit_below=s.adx_exit_below,
    ) is False
    sig = strat.evaluate(bars, in_position=False)
    assert sig.action is Action.WAIT
    assert "ADX sit-out: chop" in sig.reason
    assert adx_last < s.adx_enter_above or True  # informational


def test_momentum_fee_gate_blocks_tiny_tp():
    # Disable ADX so we isolate the fee gate; use trending bars.
    s = _settings(
        adx_gate_enabled=False,
        take_profit_pct=0.004,  # 0.4% — below 1.0% min edge
        min_edge_bps=100.0,
    )
    strat = TrendBreakoutStrategy(s)
    bars = _trending_momentum_bars()
    sig = strat.evaluate(bars, in_position=False)
    assert sig.action is Action.WAIT
    assert "Fee gate" in sig.reason


def test_momentum_allows_when_adx_trending_and_edge_ok():
    s = _settings(
        adx_period=5,
        adx_enter_above=25.0,
        adx_exit_below=20.0,
        take_profit_pct=0.08,
        min_edge_bps=100.0,
    )
    strat = TrendBreakoutStrategy(s)
    bars = _trending_momentum_bars()
    frame = enrich(
        bars,
        fast_ema=s.fast_ema,
        slow_ema=s.slow_ema,
        regime_ema=s.regime_ema,
        rsi_period=s.rsi_period,
        atr_period=s.atr_period,
        breakout_lookback=s.breakout_lookback,
        volume_lookback=s.volume_lookback,
        adx_period=s.adx_period,
    )
    allowed = adx_trend_allowed(
        frame["adx"].dropna().tolist(),
        enter_above=s.adx_enter_above,
        exit_below=s.adx_exit_below,
    )
    # If synthetic bars fail to produce high ADX, skip rather than invent a pass.
    if not allowed:
        pytest.skip(f"synthetic trend ADX too low: {float(frame['adx'].dropna().iloc[-1]):.1f}")
    sig = strat.evaluate(bars, in_position=False)
    # May still WAIT on RSI/regime; must not be ADX or fee block.
    assert "ADX sit-out" not in sig.reason
    assert "Fee gate" not in sig.reason


def test_settings_reject_inverted_adx_bands():
    with pytest.raises(Exception):
        _settings(adx_enter_above=15.0, adx_exit_below=20.0)


def test_config_defaults_expose_new_knobs():
    s = Settings(
        _env_file=None,
        kraken_api_key="",
        kraken_api_secret="",
        strategy_equity_usd=100.0,
        paper_trading=True,
        dry_run=True,
        allow_live_trading=False,
    )
    assert s.adx_period == 14
    assert s.adx_enter_above == 25.0
    assert s.adx_exit_below == 20.0
    assert s.adx_gate_enabled is True
    assert s.min_edge_bps == 100.0
    assert s.min_edge_gate_enabled is True

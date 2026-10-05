from pathlib import Path

import pytest

from dublin_bot.config import Settings


def test_safe_defaults_are_paper_and_dry_run():
    settings = Settings(_env_file=None)
    assert settings.paper_trading is True
    assert settings.dry_run is True
    assert settings.allow_live_trading is False
    assert settings.strategy_equity_usd == 100.0
    assert settings.risk_per_trade == 0.01
    assert settings.max_position_fraction == 0.25
    assert settings.max_orders_per_day == 0
    assert settings.universe_mode == "all_usd"
    assert settings.strategy == "regime_trend"
    assert settings.max_leverage == 1.0
    assert settings.margin_exposure_fraction == 0.0
    assert settings.auto_start_monitor is False
    assert settings.timeframe_minutes == 15
    assert settings.monitor_interval_seconds == 900
    assert settings.cooldown_minutes == 10
    assert settings.rapid_mode is True
    # Conservative risk defaults (ported from the Dublin repo).
    assert settings.max_leverage == 1.0
    assert settings.margin_exposure_fraction == 0.0
    assert settings.risk_per_trade == 0.01
    assert settings.max_position_fraction == 0.25


def test_live_mode_requires_explicit_acknowledgement():
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            paper_trading=False,
            allow_live_trading=True,
            live_risk_acknowledgement="",
        )


def test_example_environment_preserves_conservative_setup_profile():
    example = Path(__file__).resolve().parents[1] / ".env.example"
    settings = Settings(_env_file=example)
    assert settings.safety_locked is True
    assert settings.strategy_equity_usd == 25.0
    assert settings.risk_per_trade == 0.01
    assert settings.max_position_fraction == 0.25
    assert settings.max_orders_per_day == 3
    assert settings.universe_mode == "basket"
    assert settings.strategy == "momentum"
    assert settings.cooldown_minutes == 15
    assert settings.auto_start_monitor is False

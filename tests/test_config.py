import pytest

from dublin_bot.config import Settings


def test_safe_defaults_are_paper_and_dry_run():
    settings = Settings(_env_file=None)
    assert settings.paper_trading is True
    assert settings.dry_run is True
    assert settings.allow_live_trading is False
    assert settings.strategy_equity_usd == 100.0
    assert settings.auto_start_monitor is False
    assert settings.timeframe_minutes == 15
    assert settings.monitor_interval_seconds == 900
    assert settings.cooldown_minutes == 10
    assert settings.rapid_mode is True


def test_live_mode_requires_explicit_acknowledgement():
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            paper_trading=False,
            allow_live_trading=True,
            live_risk_acknowledgement="",
        )

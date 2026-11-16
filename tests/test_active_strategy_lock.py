"""Regression: MAYO V1 defaults to regular-spot 1h regime research only."""
from dublin_bot.config import Settings, paper_live_choice_ok

def test_regime_v1_default_locked_universe():
    s = Settings(_env_file=None)
    assert s.strategy == "regime_trend"
    assert s.timeframe_minutes == 60
    assert s.universe_mode == "basket"
    assert s.coin_basket == ["BTC/USD", "ETH/USD", "SOL/USD"]
    assert s.universe_allowlist == ["BTC/USD", "ETH/USD", "SOL/USD"]
    assert s.paper_trading and s.dry_run and not s.allow_live_trading
    assert not s.meanrev_sleeve_enabled and not s.trendhold_sleeve_enabled
    assert paper_live_choice_ok(s)[0]

def test_regime_v1_rejects_legacy_market_in_active_paper_loop():
    s = Settings(_env_file=None, universe_allowlist=["BTC/USD", "PUMP/USD"])
    allowed, reason = paper_live_choice_ok(s)
    assert not allowed
    assert "outside" in reason

def test_regime_v1_rejects_broad_market_and_faster_cadence():
    s = Settings(_env_file=None, universe_mode="all_usd")
    assert not paper_live_choice_ok(s)[0]
    s = Settings(_env_file=None, day_trade_mode=True)
    assert not paper_live_choice_ok(s)[0]

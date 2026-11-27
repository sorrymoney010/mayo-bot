from pathlib import Path

from dublin_bot.config import Settings, paper_live_choice_ok

TEMPLATE = Path(__file__).resolve().parents[1] / ".env.binance.example"


def test_binance_template_is_the_researched_config_and_locked():
    s = Settings(_env_file=str(TEMPLATE))
    assert s.broker == "binance" and s.strategy == "regime_trend"
    assert s.timeframe_minutes == 240
    assert [x.upper() for x in s.universe_allowlist] == ["BTC/USD", "ETH/USD"]   # SOL had no edge
    assert s.universe_mode == "basket"
    assert s.meanrev_sleeve_enabled is False and s.trendhold_sleeve_enabled is False
    assert (s.stop_loss_pct, s.take_profit_pct) == (0.05, 0.25)                 # tested exits
    assert s.use_bracket is False and s.order_type == "market" and s.binance_exchange_stop is True
    assert s.strategy_equity_usd == 20.0 and s.max_orders_per_day == 3 and s.max_leverage == 1.0
    assert paper_live_choice_ok(s)[0] is True
    # shipped locked: nothing can trade live until the operator flips all four flags
    assert s.safety_locked is True
    assert s.paper_trading and s.dry_run and not s.allow_live_trading

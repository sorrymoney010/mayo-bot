"""Regression: DCA symbol swap must be restored even if risk rejects the order."""

from __future__ import annotations

from unittest.mock import MagicMock

from dublin_bot.config import Settings
from dublin_bot.dca import DCAState
from dublin_bot.engine import TradingEngine
from dublin_bot.models import Action, RiskDecision, Signal


def _fake_bars():
    import pandas as pd
    idx = pd.date_range("2026-01-01", periods=120, freq="15min", tz="UTC")
    return pd.DataFrame(
        {"close": [1.0] * 120, "open": [1.0] * 120, "high": [1.0] * 120,
         "low": [1.0] * 120, "volume": [1.0] * 120, "vwap": [1.0] * 120,
         "rsi": [70.0] * 120, "ema_slow": [1.0] * 120, "ema_fast": [1.0] * 120,
         "ema_regime": [1.0] * 120},
        index=idx,
    )


def make_engine():
    s = Settings(_env_file=None, dca_enabled=True, dca_symbol="PUMP/USD",
                 dca_interval_minutes=30, dca_fixed_usd=2.0,
                 dca_max_buys_per_day=10, dca_max_total_buys=100,
                 symbol="XRP/USD")
    gw = MagicMock()
    gw.get_ticker_for.return_value = {"last": 1.23, "bid": 1.22, "ask": 1.24,
                                      "volume_24h": 1e6, "vwap_24h": 1.23, "trades_24h": 100}
    gw.market_quality.return_value = {"bid": 1.22, "ask": 1.24, "recent_dollar_volume": 1e6}
    gw.has_position.return_value = False
    gw.account_equity.return_value = 7.0
    gw.get_bars.return_value = _fake_bars()
    eng = TradingEngine(s, gateway=gw)
    # Force a DCA signal to be due: last buy long past the 30m interval.
    from datetime import datetime, timedelta, timezone
    st = DCAState()
    st.last_buy_at = datetime.now(timezone.utc) - timedelta(minutes=31)
    eng._dca_state = st
    return eng


def test_dca_symbol_restored_when_risk_rejects():
    """If the risk manager rejects a DCA BUY, the engine must NOT stay on PUMP/USD."""
    eng = make_engine()
    eng.risk.evaluate = lambda *a, **k: RiskDecision(False, "rejected by test")
    eng.strategy.evaluate = lambda *a, **k: Signal(Action.WAIT, 0, "wait", price=1.0)
    eng.run_cycle()
    assert eng.settings.symbol == "XRP/USD", f"symbol stuck on {eng.settings.symbol}"


def test_dca_symbol_restored_after_successful_buy():
    eng = make_engine()
    eng.run_cycle()
    assert eng.settings.symbol == "XRP/USD", f"symbol stuck on {eng.settings.symbol}"

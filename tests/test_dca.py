"""Tests for the DCA accumulator sleeve (logic only, no network)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dublin_bot.config import Settings
from dublin_bot.dca import DCAAccumulator, DCAState
from dublin_bot.models import Action


def make_settings(**overrides) -> Settings:
    base = dict(dca_enabled=True, dca_symbol="PUMP/USD", dca_interval_minutes=240,
                dca_fixed_usd=2.0, dca_max_buys_per_day=4, dca_max_total_buys=40,
                dca_stop_buffer=0.05)
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_disabled_returns_none():
    acc = DCAAccumulator(make_settings(dca_enabled=False))
    assert acc.maybe_signal(DCAState(), price=1.0, in_position=False, mr_action_is_buy=False) is None


def test_skips_when_in_position():
    acc = DCAAccumulator(make_settings())
    assert acc.maybe_signal(DCAState(), price=1.0, in_position=True, mr_action_is_buy=False) is None


def test_skips_when_mr_is_buying():
    acc = DCAAccumulator(make_settings())
    assert acc.maybe_signal(DCAState(), price=1.0, in_position=False, mr_action_is_buy=True) is None


def test_first_buy_due():
    acc = DCAAccumulator(make_settings())
    sig = acc.maybe_signal(DCAState(), price=1.23, in_position=False, mr_action_is_buy=False)
    assert sig is not None
    assert sig.action is Action.BUY
    assert sig.stop_price == 1.23 * (1 - 0.05)


def test_interval_blocks_rapid_repeat():
    acc = DCAAccumulator(make_settings(dca_interval_minutes=240))
    st = DCAState()
    now = datetime.now(timezone.utc)
    # first buy
    assert acc.maybe_signal(st, price=1.0, in_position=False, mr_action_is_buy=False) is not None
    acc.record_buy(st)
    # immediately after, interval not elapsed
    assert acc.maybe_signal(st, price=1.0, in_position=False, mr_action_is_buy=False) is None
    # simulate time passing beyond interval
    st.last_buy_at = now - timedelta(minutes=241)
    assert acc.maybe_signal(st, price=1.0, in_position=False, mr_action_is_buy=False) is not None


def test_daily_cap_blocks():
    acc = DCAAccumulator(make_settings(dca_max_buys_per_day=2))
    st = DCAState()
    st.buys_today = 2
    assert acc.maybe_signal(st, price=1.0, in_position=False, mr_action_is_buy=False) is None


def test_total_cap_blocks():
    acc = DCAAccumulator(make_settings(dca_max_total_buys=3))
    st = DCAState()
    st.total_buys = 3
    assert acc.maybe_signal(st, price=1.0, in_position=False, mr_action_is_buy=False) is None


def test_record_buy_updates_counters():
    acc = DCAAccumulator(make_settings())
    st = DCAState()
    acc.record_buy(st)
    assert st.buys_today == 1
    assert st.total_buys == 1
    assert st.last_buy_at is not None

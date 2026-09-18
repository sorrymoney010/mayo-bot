"""Tests for day-trade mode (B1) and the real-time WebSocket feed (B2)."""

from __future__ import annotations

from unittest.mock import MagicMock

from dublin_bot.config import Settings
from dublin_bot.engine import TradingEngine
from dublin_bot.dashboard import TradingMonitor
from dublin_bot.realtime import KrakenRealtimeFeed, FeedSnapshot


def test_day_trade_mode_applies_5m_candle_cadence() -> None:
    s = Settings(day_trade_mode=True, monitor_interval_seconds=900)
    eng = TradingEngine(s)
    assert eng.settings.timeframe_minutes == 5
    assert eng.settings.monitor_interval_seconds == 300


def test_monitor_runs_two_seconds_after_next_five_minute_close() -> None:
    settings = Settings(timeframe_minutes=5, candle_close_delay_seconds=2)
    monitor = TradingMonitor(settings)
    # 12:03:20 in an arbitrary five-minute bucket -> next run at 12:05:02.
    assert monitor._seconds_until_next_candle(now=200.0) == 102.0
    assert monitor.interval_seconds == 300


def test_normal_mode_keeps_defaults() -> None:
    s = Settings(day_trade_mode=False, timeframe_minutes=15, monitor_interval_seconds=900)
    eng = TradingEngine(s)
    assert eng.settings.timeframe_minutes == 15
    assert eng.settings.monitor_interval_seconds == 900


def test_realtime_feed_falls_back_to_rest_when_no_ws() -> None:
    feed = KrakenRealtimeFeed({"BTC/USD": "XBT/USD"})
    # No socket started -> latest() returns the empty snapshot.
    snap = feed.latest("BTC/USD")
    assert isinstance(snap, FeedSnapshot)
    assert snap.source == "rest"


def test_realtime_feed_ingest_updates_snapshot() -> None:
    feed = KrakenRealtimeFeed({"BTC/USD": "XBT/USD"})
    # Simulate a Kraken ticker WS message: [chan, {c:[last],b:[bid],a:[ask]},
    #                                       "ticker", "XBT/USD"]
    msg = '[1, {"c":["60000.5"], "b":["60000.0"], "a":["60001.0"]}, "ticker", "XBT/USD"]'
    feed._ingest(msg)
    snap = feed.latest("BTC/USD")
    assert snap is not None
    assert snap.last == 60000.5
    assert snap.bid == 60000.0
    assert snap.ask == 60001.0
    assert snap.source == "ws"


def test_engine_live_price_uses_feed_when_connected() -> None:
    s = Settings(day_trade_mode=True)
    eng = TradingEngine(s)
    # Inject a fake connected feed.
    snap = FeedSnapshot(symbol="BTC/USD", last=61000.0, source="ws")
    fake_feed = MagicMock()
    fake_feed.latest.return_value = snap
    eng._feed = fake_feed
    assert eng.live_price("BTC/USD") == 61000.0


def test_check_realtime_stop_breach() -> None:
    s = Settings(day_trade_mode=True)
    eng = TradingEngine(s)
    snap = FeedSnapshot(symbol="BTC/USD", last=59000.0, source="ws")
    fake_feed = MagicMock()
    fake_feed.latest.return_value = snap
    eng.gateway = MagicMock()
    eng.gateway.has_position.return_value = True
    eng._feed = fake_feed
    # Entry 60000, stop at 59500 -> 59000 <= 59500 => breached.
    assert eng.check_realtime_stop(60000.0, 59500.0) is True
    # Price above stop => not breached.
    snap.last = 59800.0
    assert eng.check_realtime_stop(60000.0, 59500.0) is False

import json
from pathlib import Path

import pandas as pd

from dublin_bot import intraday_sleeve as m
from dublin_bot.pipeline import binanceus as b
from dublin_bot.pipeline.tickstore import TickStore


class _Resp:
    def __init__(self, data, code=200):
        self._d, self.status_code = data, code

    def json(self):
        return self._d

    def raise_for_status(self):
        pass


class _Sess:
    def __init__(self, pages):
        self.pages, self.calls, self.headers = list(pages), [], {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params)))
        return _Resp(self.pages.pop(0) if self.pages else [])


def _agg(i, t, m_=False):
    return {"a": i, "p": "100.5", "q": "0.1", "T": t, "m": m_}


def test_venue_default_binanceus_and_dirs(monkeypatch, tmp_path):
    monkeypatch.delenv("MARKET_VENUE", raising=False)
    assert b.venue() == "binanceus"
    assert b.venue_data_dir(tmp_path) == tmp_path / "binanceus"
    monkeypatch.setenv("MARKET_VENUE", "kraken")
    assert b.venue_data_dir(tmp_path) == tmp_path
    assert b.bsym("XBTUSD") == "BTCUSDT" and b.bsym("SOL/USD") == "SOLUSDT"


def test_public_trades_time_then_id_cursor():
    s = _Sess([[], [_agg(7, 1_700_000_000_000), _agg(8, 1_700_000_001_000, True)], [_agg(9, 1_700_000_002_000)]])
    c = b.BinanceUSPublic(session=s, min_interval=0, sleep=lambda x: None, now_fn=lambda: 1_700_010_000)
    ticks, cur = c.trades("XBTUSD", "1699990000")
    assert [t.trade_id for t in ticks] == [7, 8] and cur == "id:9"
    assert ticks[1].side == "s" and ticks[0].side == "b"  # m=True -> aggressor sold
    assert s.calls[0][1]["endTime"] - s.calls[0][1]["startTime"] < 3_600_000  # 1h windows, advanced on empty
    assert all("api.binance.us/api/v3/aggTrades" in u for u, _ in s.calls) and "X-MBX-APIKEY" not in s.headers
    ticks, cur = c.trades("XBTUSD", cur)
    assert s.calls[-1][1]["fromId"] == 9 and ticks[0].trade_id == 9


def test_collector_parses_streams_and_marks_live(tmp_path):
    now = [1_700_000_000.0]
    c = b.BinanceUSCollector(TickStore(tmp_path), client=b.BinanceUSPublic(session=_Sess([])),
                             now_fn=lambda: now[0], log=lambda m: None)
    assert c.ws_url.startswith("wss://stream.binance.us/stream?streams=btcusdt@aggTrade")
    assert c.subscribe_messages() == []
    c.handle(json.dumps({"stream": "ethusdt@bookTicker", "data": {"s": "ETHUSDT", "b": "2000", "a": "2001"}}))
    assert c.st["ETH/USD"]["quotes"] and not c.st["ETH/USD"]["in_sync"]  # not before REST catch-up
    c.handle(json.dumps({"stream": "btcusdt@aggTrade", "data": {"e": "aggTrade", "s": "BTCUSDT", **_agg(5, 1_700_000_000_000)}}))
    c.handle(json.dumps({"stream": "btcusdt@aggTrade", "data": {"e": "aggTrade", "s": "BTCUSDT", **_agg(5, 1_700_000_000_000)}}))
    assert len(c.st["BTC/USD"]["buf"]) == 1 and c.st["BTC/USD"]["in_sync"]
    c.connected = True
    c.flush(force=True)
    st = json.loads((Path(tmp_path) / "ticks" / "BTCUSD" / "_status.json").read_text())
    assert st["in_sync"] and st["last_trade_id"] == 5


def test_sleeve_uses_binanceus_fees_and_tags_venue(tmp_path, monkeypatch):
    monkeypatch.delenv("MARKET_VENUE", raising=False)
    monkeypatch.setenv("INTRADAY_MAKER_FEE_BPS", "40")  # Kraken settings must not leak into Binance.US
    s = m.IntradaySleeve(root=tmp_path)
    assert (s.venue, s.maker, s.taker, s.slip) == ("binanceus", 0.0, 2.0, 3.0)
    s._event(s.events_path, {"event": "x"})
    assert json.loads(s.events_path.read_text().splitlines()[-1])["venue"] == "binanceus"
    monkeypatch.setenv("MARKET_VENUE", "kraken")
    k = m.IntradaySleeve(root=tmp_path)
    assert (k.maker, k.taker) == (40.0, 80.0)


def test_klines_closed_only():
    rows = [[1_700_000_000_000 + i * 900_000, "1", "2", "0.5", "1.5", "10"] for i in range(3)]
    c = b.BinanceUSPublic(session=_Sess([rows]), min_interval=0, now_fn=lambda: 1_700_000_000 + 2 * 900 + 60)
    df = c.klines("BTC/USD", 15)
    assert len(df) == 2 and isinstance(df.index, pd.DatetimeIndex)

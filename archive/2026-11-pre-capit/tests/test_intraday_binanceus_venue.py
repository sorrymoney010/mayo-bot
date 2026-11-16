"""Archived from tests/test_binanceus_feed.py (intraday sleeve retired 2026-11-15)."""
import json

from dublin_bot import intraday_sleeve as m


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



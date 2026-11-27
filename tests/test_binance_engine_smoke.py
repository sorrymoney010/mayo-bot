"""A full engine cycle on BinanceGateway (fake exchange, dry-run): proves the adapter honors the
interface the engine actually uses, with no network and no orders."""
from __future__ import annotations

import time

import numpy as np

from dublin_bot.binance_gateway import BinanceGateway
from dublin_bot.config import Settings
from dublin_bot.engine import TradingEngine


class Resp:
    def __init__(self, d): self.d = d
    def json(self): return self.d
    def raise_for_status(self): pass


class FakeBinance:
    def __init__(self):
        now = int(time.time() // 3600 * 3600) - 3600
        x = np.arange(300, dtype=float)
        close = 60000 + 400 * np.sin(x / 7.0)
        self.klines = [[(now - (299 - i) * 3600) * 1000, str(c), str(c + 50), str(c - 50), str(c),
                        "10", 0, "0", 0, "0", "0", "0"] for i, c in enumerate(close)]
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        path = url.split("?")[0].replace("https://api.binance.us", "")
        self.calls.append(("GET", path))
        if path == "/api/v3/klines": return Resp(self.klines)
        if path == "/api/v3/exchangeInfo":
            return Resp({"symbols": [{"baseAsset": "BTC", "quoteAsset": "USDT", "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.00001000", "minQty": "0.00001000"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
                {"filterType": "NOTIONAL", "minNotional": "5"}]}]})
        if path == "/api/v3/ticker/24hr":
            return Resp({"lastPrice": "60000", "bidPrice": "59999", "askPrice": "60001", "quoteVolume": "9e8"})
        if path in ("/api/v3/ping",): return Resp({})
        if path == "/api/v3/time": return Resp({"serverTime": int(time.time() * 1000)})
        if path == "/api/v3/ticker/price": return Resp([{"symbol": "BTCUSDT", "price": "60000"}])
        raise AssertionError(f"unexpected GET {path}")

    def post(self, *a, **k): raise AssertionError("an order was POSTed in dry-run")
    def delete(self, *a, **k): raise AssertionError("a DELETE was sent in dry-run")


def test_full_cycle_on_binance_dry_run_places_no_orders(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MAYO_LEDGER_OWNER", "1")
    (tmp_path / "logs").mkdir()
    s = Settings(_env_file=None, broker="binance", symbol="BTC/USD", strategy="regime_trend",
                 timeframe_minutes=60, use_bracket=False, order_type="market",
                 strategy_equity_usd=25.0, max_orders_per_day=3, min_dollar_volume=0.0,
                 universe_allowlist=["BTC/USD"], learner_priors_path="none.json",
                 audit_log_path=str(tmp_path / "audit.jsonl"), journal_path=tmp_path / "j.jsonl",
                 session_state_path=str(tmp_path / "state.json"),
                 idempotency_path=str(tmp_path / "idem.json"))
    sess = FakeBinance()
    gw = BinanceGateway(s, session=sess, allow_order_submission=False)
    eng = TradingEngine(s, gateway=gw)
    rec = eng.run_once()
    assert rec is not None
    assert not any(v == "POST" for v, _ in sess.calls)

"""Binance.US adapter, simulator and live runner, against an in-memory exchange."""
from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from dublin_bot import alpaca_paper as ap
from dublin_bot.alpaca_paper import RunnerConfig, SleeveRunner
from dublin_bot.binance_gateway import BinanceGateway
from dublin_bot.binanceus_gateway import BinanceUSGateway, interval_str
from dublin_bot.config import Settings
from dublin_bot.errors import BrokerError, SafetyLockError
from dublin_bot.sim_execution import SimulatedExecution

T0 = datetime(2026, 11, 18, 12, 0, 30, tzinfo=timezone.utc)
ACK = "I_ACCEPT_LIVE_TRADING_RISK"


class Resp:
    def __init__(self, code, body):
        self.status_code, self._b = code, body
        self.text = json.dumps(body)

    def json(self):
        return self._b


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class FakeBinanceUS:
    """Enough of api.binance.us spot v3 for the adapter and runner."""

    FEE = 0.0002   # 0.02% taker, charged in the received asset on buys

    def __init__(self, clock):
        self.clock = clock
        self.price = {"BTCUSD": 100_000.0, "ETHUSD": 4_000.0, "SOLUSD": 200.0}
        self.bal = {"USD": 1_000.0, "BTC": 0.0, "ETH": 0.0, "SOL": 0.0}
        self.orders: dict[int, dict] = {}
        self.posts: list[dict] = []
        self.tests: list[dict] = []
        self.next_id = 1
        self.klines: dict[tuple[str, str], list[list]] = {}
        self.minute_lows: list[tuple[int, float, float]] = []   # (open_ms, low, high)
        self.signed_calls: list[tuple[str, dict]] = []

    def base(self, pair):
        return pair[:-3]

    def set_klines(self, pair, interval, minutes, n=300):
        end_ms = int(self.clock().timestamp() // 60 * 60 * 1000)
        step = minutes * 60_000
        rows = []
        for i in range(n):
            o = end_ms - step * (n - i)
            p = self.price[pair]
            rows.append([o, str(p), str(p * 1.001), str(p * 0.999), str(p), "10", o + step - 1, "0", 5])
        self.klines[(pair, interval)] = rows

    def locked(self, asset):
        return sum(float(o["origQty"]) - float(o["executedQty"]) for o in self.orders.values()
                   if o["status"] == "NEW" and o["side"] == "SELL" and self.base(o["symbol"]) == asset)

    def _fill(self, o, px):
        q = float(o["origQty"])
        b = self.base(o["symbol"])
        if o["side"] == "BUY":
            self.bal["USD"] -= q * px
            self.bal[b] += q * (1 - self.FEE)
        else:
            self.bal[b] -= q
            self.bal["USD"] += q * px * (1 - self.FEE)
        o.update(status="FILLED", executedQty=str(q), cummulativeQuoteQty=str(q * px))

    def move(self, pair, px):
        """Price moves to px; resting orders react like the matching engine."""
        self.price[pair] = px
        for o in self.orders.values():
            if o["status"] != "NEW" or o["symbol"] != pair:
                continue
            if o["type"] == "STOP_LOSS_LIMIT" and px <= float(o["stopPrice"]):
                if px >= float(o["price"]):
                    self._fill(o, float(o["stopPrice"]))
                else:
                    o["type"] = "LIMIT"          # triggered, gapped below its limit
            elif o["type"] == "LIMIT" and o["side"] == "BUY" and px <= float(o["price"]):
                self._fill(o, float(o["price"]))

    def request(self, method, url, params=None, headers=None, timeout=None, json=None):
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        q = dict(urllib.parse.parse_qsl(parsed.query)) if parsed.query else {}
        q.update({k: str(v) for k, v in (params or {}).items()})
        if "signature" in q:
            assert headers and headers.get("X-MBX-APIKEY") == "key"
            assert "timestamp" in q and "recvWindow" in q
            self.signed_calls.append((method + " " + path, q))
        if path == "/api/v3/ping":
            return Resp(200, {})
        if path == "/api/v3/time":
            return Resp(200, {"serverTime": int(self.clock().timestamp() * 1000)})
        if path == "/api/v3/klines":
            if q["interval"] == "1m":
                since = int(q["startTime"])
                rows = [[o, "0", str(h), str(lo), "0", "0", o + 59_999] for o, lo, h in self.minute_lows if o >= since]
                return Resp(200, rows)
            rows = [r for r in self.klines.get((q["symbol"], q["interval"]), [])
                    if int(q.get("startTime", 0)) <= r[0] <= int(q.get("endTime", 10**15))]
            return Resp(200, rows[: int(q.get("limit", 500))])
        if path == "/api/v3/ticker/bookTicker":
            p = self.price[q["symbol"]]
            return Resp(200, {"bidPrice": str(p * 0.9999), "askPrice": str(p * 1.0001)})
        if path == "/api/v3/ticker/price":
            return Resp(200, {"price": str(self.price[q["symbol"]])})
        if path == "/api/v3/exchangeInfo":
            sym = q["symbol"]
            if sym not in self.price:
                return Resp(400, {"code": -1121, "msg": "Invalid symbol."})
            step = {"BTCUSD": "0.00001", "ETHUSD": "0.0001", "SOLUSD": "0.001"}[sym]
            return Resp(200, {"symbols": [{
                "symbol": sym, "status": "TRADING", "baseAsset": self.base(sym), "quoteAsset": "USD",
                "orderTypes": ["LIMIT", "LIMIT_MAKER", "MARKET", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"],
                "filters": [
                    {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000", "tickSize": "0.01"},
                    {"filterType": "LOT_SIZE", "minQty": step, "maxQty": "9000", "stepSize": step},
                    {"filterType": "MARKET_LOT_SIZE", "minQty": step, "maxQty": "9000", "stepSize": step},
                    {"filterType": "NOTIONAL", "minNotional": "10.00", "applyMinToMarket": True},
                ]}]})
        if path == "/api/v3/account":
            return Resp(200, {"canTrade": True, "canWithdraw": False,
                              "commissionRates": {"maker": "0.00000000", "taker": "0.00020000"},
                              "balances": [{"asset": a, "free": str(v - self.locked(a)),
                                            "locked": str(self.locked(a))} for a, v in self.bal.items()]})
        if path == "/api/v3/order/test":
            self.tests.append(q)
            return Resp(200, {})
        if path == "/api/v3/order" and method == "POST":
            self.posts.append(q)
            if q["side"] == "SELL" and float(q["quantity"]) > self.bal[self.base(q["symbol"])] - self.locked(self.base(q["symbol"])) + 1e-12:
                return Resp(400, {"code": -2010, "msg": "Account has insufficient balance for requested action."})
            oid = self.next_id
            self.next_id += 1
            o = {"orderId": oid, "clientOrderId": q["newClientOrderId"], "symbol": q["symbol"],
                 "side": q["side"], "type": q["type"], "status": "NEW", "origQty": q["quantity"],
                 "price": q.get("price", "0"), "stopPrice": q.get("stopPrice", "0"),
                 "executedQty": "0", "cummulativeQuoteQty": "0"}
            self.orders[oid] = o
            if q["type"] == "MARKET":
                p = self.price[q["symbol"]]
                self._fill(o, p * (1.0001 if q["side"] == "BUY" else 0.9999))
            elif q["type"] == "LIMIT" and q["side"] == "BUY" and self.price[q["symbol"]] <= float(q["price"]):
                self._fill(o, float(q["price"]))
            return Resp(200, dict(o))
        if path == "/api/v3/order" and method == "GET":
            if "orderId" in q:
                return Resp(200, dict(self.orders[int(q["orderId"])]))
            for o in self.orders.values():
                if o["clientOrderId"] == q["origClientOrderId"]:
                    return Resp(200, dict(o))
            return Resp(400, {"code": -2013, "msg": "Order does not exist."})
        if path == "/api/v3/order" and method == "DELETE":
            o = self.orders[int(q["orderId"])]
            if o["status"] != "NEW":
                return Resp(400, {"code": -2011, "msg": "Unknown order sent."})
            o["status"] = "CANCELED"
            return Resp(200, dict(o))
        if path == "/api/v3/openOrders":
            return Resp(200, [o for o in self.orders.values() if o["status"] == "NEW"])
        raise AssertionError(f"unexpected {method} {path}")


def _settings(tmp_path, live=False, **kw):
    base = dict(_env_file=None, binance_api_key="key", binance_api_secret="secret",
                audit_log_path=tmp_path / "audit.jsonl")
    if live:
        base.update(allow_live_trading=True, paper_trading=False, dry_run=False,
                    live_risk_acknowledgement=ACK)
    base.update(kw)
    return Settings(**base)


def _gw(tmp_path, clock, fake, live=True, allow=True):
    return BinanceUSGateway(_settings(tmp_path, live=live), session=fake,
                            allow_order_submission=allow, clock=clock)


# ── adapter ──────────────────────────────────────────────

def test_usd_pairs_and_filters(tmp_path):
    clock = Clock(T0)
    gw = _gw(tmp_path, clock, FakeBinanceUS(clock))
    assert gw.pair("BTC/USD") == "BTCUSD" and gw.pair("sol/usd") == "SOLUSD"
    m = gw.asset("BTC/USD")
    assert m.base == "BTC" and m.min_notional == Decimal("10.00") and "STOP_LOSS_LIMIT" in m.order_types
    assert m.floor_qty(0.000129) == Decimal("0.00012")
    assert not m.notional_ok(Decimal("0.00005"), 100_000, market=True)
    assert interval_str(240) == "4h"


def test_live_orders_need_every_lock(tmp_path):
    clock = Clock(T0)
    fake = FakeBinanceUS(clock)
    paper_settings = _gw(tmp_path, clock, fake, live=False)
    assert not paper_settings.order_submission_enabled
    with pytest.raises(SafetyLockError):
        paper_settings.submit_order(symbol="BTC/USD", side="buy", order_type="market",
                                    qty=Decimal("0.0002"), client_order_id="dub-x")
    no_perm = _gw(tmp_path, clock, fake, live=True, allow=False)
    with pytest.raises(SafetyLockError):
        no_perm.submit_order(symbol="BTC/USD", side="buy", order_type="market",
                             qty=Decimal("0.0002"), client_order_id="dub-x")
    assert fake.posts == []


def test_signed_order_is_idempotent_and_client_id_checked(tmp_path):
    clock = Clock(T0)
    fake = FakeBinanceUS(clock)
    gw = _gw(tmp_path, clock, fake)
    a = gw.submit_order(symbol="BTC/USD", side="buy", order_type="market", qty=Decimal("0.0002"),
                        client_order_id="dub-r1-BTCUSD-1-e")
    b = gw.submit_order(symbol="BTC/USD", side="buy", order_type="market", qty=Decimal("0.0002"),
                        client_order_id="dub-r1-BTCUSD-1-e")
    assert a["order_id"] == b["order_id"] and len(fake.posts) == 1
    assert a["status"] == "filled" and a["filled_avg_price"] == pytest.approx(100_010.0)
    with pytest.raises(ValueError):
        gw.check_client_id("x" * 37)
    with pytest.raises(ValueError):
        gw.check_client_id("bad id!")


def test_below_min_notional_is_refused_before_sending(tmp_path):
    clock = Clock(T0)
    fake = FakeBinanceUS(clock)
    gw = _gw(tmp_path, clock, fake)
    with pytest.raises(ValueError, match="notional"):
        gw.submit_order(symbol="BTC/USD", side="buy", order_type="market", qty=Decimal("0.00005"),
                        client_order_id="dub-small")
    assert fake.posts == []


def test_test_order_places_nothing(tmp_path):
    clock = Clock(T0)
    fake = FakeBinanceUS(clock)
    gw = _gw(tmp_path, clock, fake, live=False, allow=False)
    p = gw.test_order(symbol="BTC/USD", side="sell", order_type="stop_limit", qty=Decimal("0.0002"),
                      stop_price=97_000, limit_price=96_030, client_order_id="dub-t")
    assert p["type"] == "STOP_LOSS_LIMIT" and p["stopPrice"] == "97000.00"
    assert len(fake.tests) == 1 and fake.posts == [] and fake.orders == {}


def test_fetch_bars_returns_closed_bars_only(tmp_path):
    clock = Clock(T0)
    fake = FakeBinanceUS(clock)
    fake.set_klines("BTCUSD", "4h", 240, n=300)
    rows = fake.klines[("BTCUSD", "4h")]
    # add a still-forming bar
    o = rows[-1][0] + 240 * 60_000
    rows.append([o, "1", "1", "1", "1", "1", o + 240 * 60_000 - 1, "0", 1])
    gw = _gw(tmp_path, clock, fake)
    df = gw.fetch_bars("BTC/USD", 240, bars=250)
    assert len(df) == 250
    assert (df["time"] + 240 * 60).max() <= T0.timestamp() + 1


def test_engine_binance_path_refuses_kraken_shaped_orders(tmp_path):
    s = _settings(tmp_path, live=True)
    gw = BinanceGateway(s, allow_order_submission=True)
    with pytest.raises(BrokerError, match="Kraken-format"):
        gw.add_order({"ordertype": "limit", "volume": "0.001", "price": "1"}, signal_price=1.0)


# ── live runner on the fake exchange ─────────────────────

def _signal(entry=False, exit_=False, reason=""):
    def f(spec, raw):
        return {"bar": float(raw["time"].iloc[-1]), "close": float(raw["close"].iloc[-1]),
                "warm": True, "entry": entry, "exit": exit_, "exit_reason": reason}
    return f


def _live_runner(tmp_path, clock, fake, sleeves=("regime_1h",), symbols=("BTC/USD",), **kw):
    gw = _gw(tmp_path, clock, fake)
    cfg = RunnerConfig(state_path=tmp_path / "state.json", journal_path=tmp_path / "trades.jsonl",
                       events_path=tmp_path / "events.jsonl", sleeves=sleeves, symbols=symbols,
                       budget_usd=kw.pop("budget_usd", 1000.0), taker_bps=2.0, maker_bps=0.0, **kw)
    return SleeveRunner(gw, cfg, clock=clock, live=True), gw


def _bars(fake, clock):
    for pair in fake.price:
        fake.set_klines(pair, "1h", 60)
        fake.set_klines(pair, "4h", 240)


def _tick(fake, clock, minutes=1):
    clock.t += timedelta(minutes=minutes)
    _bars(fake, clock)


def test_runner_refuses_real_money_without_live_flag_or_locks(tmp_path):
    clock = Clock(T0)
    fake = FakeBinanceUS(clock)
    with pytest.raises(RuntimeError, match="live=True"):
        SleeveRunner(_gw(tmp_path, clock, fake), RunnerConfig(state_path=tmp_path / "s.json"))
    with pytest.raises(RuntimeError, match="refused"):
        SleeveRunner(_gw(tmp_path, clock, fake, live=False), RunnerConfig(state_path=tmp_path / "s.json"),
                     live=True)


def test_live_entry_rests_stop_on_exchange_and_stop_fill_closes_trade(tmp_path, monkeypatch):
    clock = Clock(T0)
    fake = FakeBinanceUS(clock)
    _bars(fake, clock)
    r, _gw_ = _live_runner(tmp_path, clock, fake, max_trade_usd=25.0)
    monkeypatch.setattr(ap, "evaluate", _signal(entry=True))
    r.cycle()
    slot = r.state.slots["regime_1h|BTC/USD"]
    assert slot.status == "open"
    buy, stop = fake.posts[0], fake.posts[1]
    assert buy["type"] == "MARKET" and float(buy["quantity"]) * 100_000 <= 25.0 + 1e-6   # live cap
    assert stop["type"] == "STOP_LOSS_LIMIT" and stop["side"] == "SELL"
    assert float(stop["stopPrice"]) == pytest.approx(slot.entry_px * 0.97, rel=1e-4)
    assert float(stop["quantity"]) <= float(buy["quantity"]) * (1 - FakeBinanceUS.FEE) + 1e-12
    assert all(len(p["newClientOrderId"]) <= 36 for p in fake.posts)

    # The exchange fills the stop while the runner is "asleep": no runner sell needed.
    monkeypatch.setattr(ap, "evaluate", _signal())
    fake.move("BTCUSD", 96_900.0)
    _tick(fake, clock)
    r.cycle()
    assert slot.status == "flat"
    assert [p["type"] for p in fake.posts] == ["MARKET", "STOP_LOSS_LIMIT"]
    t = json.loads((tmp_path / "trades.jsonl").read_text().splitlines()[0])
    assert t["reason"] == "stop" and t["net_bps"] < -290


def test_signal_exit_cancels_stop_then_sells_free_balance(tmp_path, monkeypatch):
    clock = Clock(T0)
    fake = FakeBinanceUS(clock)
    _bars(fake, clock)
    r, _ = _live_runner(tmp_path, clock, fake, symbols=("ETH/USD",))
    monkeypatch.setattr(ap, "evaluate", _signal(entry=True))
    r.cycle()
    slot = r.state.slots["regime_1h|ETH/USD"]
    stop_id = int(slot.stop_order_id)
    fake.move("ETHUSD", 4_300.0)
    monkeypatch.setattr(ap, "evaluate", _signal(exit_=True, reason="chandelier"))
    _tick(fake, clock, 60)
    r.cycle()
    assert slot.status == "flat"
    assert fake.orders[stop_id]["status"] == "CANCELED"
    sell = fake.posts[-1]
    assert sell["type"] == "MARKET" and sell["side"] == "SELL"
    assert fake.bal["ETH"] == pytest.approx(0.0, abs=1e-3)     # fee dust at most
    t = json.loads((tmp_path / "trades.jsonl").read_text().splitlines()[0])
    assert t["reason"] == "chandelier" and t["gross_bps"] > 700


def test_gap_through_stop_limit_falls_back_to_market_sell(tmp_path, monkeypatch):
    clock = Clock(T0)
    fake = FakeBinanceUS(clock)
    _bars(fake, clock)
    r, _ = _live_runner(tmp_path, clock, fake, symbols=("SOL/USD",))
    monkeypatch.setattr(ap, "evaluate", _signal(entry=True))
    r.cycle()
    slot = r.state.slots["regime_1h|SOL/USD"]
    stop_id = int(slot.stop_order_id)
    monkeypatch.setattr(ap, "evaluate", _signal())
    fake.move("SOLUSD", 180.0)      # -10%: through the stop AND its limit
    assert fake.orders[stop_id]["status"] == "NEW"
    _tick(fake, clock)
    r.cycle()
    assert slot.status == "flat" and fake.orders[stop_id]["status"] == "CANCELED"
    assert fake.posts[-1]["type"] == "MARKET"
    t = json.loads((tmp_path / "trades.jsonl").read_text().splitlines()[0])
    assert t["reason"] == "stop_gap"


def test_kill_switch_blocks_new_entries(tmp_path, monkeypatch):
    clock = Clock(T0)
    fake = FakeBinanceUS(clock)
    _bars(fake, clock)
    r, _ = _live_runner(tmp_path, clock, fake)
    r.kill_file.write_text("stop")
    monkeypatch.setattr(ap, "evaluate", _signal(entry=True))
    r.cycle()
    assert fake.posts == []


def test_flatten_sells_everything_the_runner_holds(tmp_path, monkeypatch):
    clock = Clock(T0)
    fake = FakeBinanceUS(clock)
    _bars(fake, clock)
    r, _ = _live_runner(tmp_path, clock, fake, symbols=("BTC/USD", "ETH/USD"))
    monkeypatch.setattr(ap, "evaluate", _signal(entry=True))
    r.cycle()
    assert sum(1 for s in r.state.slots.values() if s.status == "open") == 2
    r.flatten()
    assert all(s.status == "flat" for s in r.state.slots.values())
    assert not [o for o in fake.orders.values() if o["status"] == "NEW"]


# ── paper simulator on real-shaped prices ─────────────────

def _sim(tmp_path, clock, fake, cash=1000.0):
    data = BinanceUSGateway(_settings(tmp_path), session=fake, clock=clock)
    return SimulatedExecution(data, state_path=tmp_path / "sim.json", starting_cash=cash,
                              taker_bps=2.0, maker_bps=0.0, slippage_bps=5.0, clock=clock)


def test_sim_limit_buy_fills_on_one_minute_low_and_locks_stop_coin(tmp_path):
    clock = Clock(T0)
    fake = FakeBinanceUS(clock)
    sim = _sim(tmp_path, clock, fake)
    o = sim.submit_order(symbol="BTC/USD", side="buy", order_type="limit", qty=Decimal("0.002"),
                         limit_price=99_900.0, client_order_id="dub-1")
    assert o["status"] == "new" and sim.cash() == pytest.approx(1000 - 0.002 * 99_900)
    clock.t += timedelta(minutes=2)
    fake.minute_lows.append((int((clock.t.timestamp() - 60) * 1000), 99_850.0, 100_100.0))
    o = sim.get_order(o["order_id"])
    assert o["status"] == "filled" and o["filled_avg_price"] == 99_900.0
    assert sim.position_qty("BTC/USD") == pytest.approx(0.002)            # 0% maker fee
    st = sim.submit_order(symbol="BTC/USD", side="sell", order_type="stop_limit", qty=Decimal("0.002"),
                          stop_price=96_900.0, limit_price=95_931.0, client_order_id="dub-2")
    assert sim.position_qty("BTC/USD") == 0.0 and sim.balance_total("BTC/USD") == pytest.approx(0.002)
    clock.t += timedelta(minutes=2)
    fake.minute_lows.append((int((clock.t.timestamp() - 60) * 1000), 96_500.0, 97_200.0))
    st = sim.get_order(st["order_id"])
    assert st["status"] == "filled" and st["filled_avg_price"] == pytest.approx(96_900.0 * 0.9995)


def test_sim_stop_limit_gap_rests_until_price_returns(tmp_path):
    clock = Clock(T0)
    fake = FakeBinanceUS(clock)
    sim = _sim(tmp_path, clock, fake)
    sim.submit_order(symbol="SOL/USD", side="buy", order_type="market", qty=Decimal("1"),
                     client_order_id="dub-b")
    q = Decimal(str(sim.position_qty("SOL/USD"))).quantize(Decimal("0.001"), rounding="ROUND_DOWN")
    st = sim.submit_order(symbol="SOL/USD", side="sell", order_type="stop_limit", qty=q,
                          stop_price=194.0, limit_price=192.06, client_order_id="dub-s")
    clock.t += timedelta(minutes=2)
    fake.minute_lows.append((int((clock.t.timestamp() - 60) * 1000), 185.0, 186.0))
    assert sim.get_order(st["order_id"])["status"] == "new"               # gapped through
    clock.t += timedelta(minutes=2)
    fake.minute_lows.append((int((clock.t.timestamp() - 60) * 1000), 190.0, 193.0))
    st = sim.get_order(st["order_id"])
    assert st["status"] == "filled" and st["filled_avg_price"] == 192.06


def test_paper_runner_end_to_end_on_simulator(tmp_path, monkeypatch):
    clock = Clock(T0)
    fake = FakeBinanceUS(clock)
    _bars(fake, clock)
    sim = _sim(tmp_path, clock, fake, cash=300.0)
    cfg = RunnerConfig(state_path=tmp_path / "state.json", journal_path=tmp_path / "trades.jsonl",
                       events_path=tmp_path / "events.jsonl", sleeves=("meanrev_4h_mk",),
                       symbols=("ETH/USD",), budget_usd=300.0, taker_bps=2.0, maker_bps=0.0)
    r = SleeveRunner(sim, cfg, clock=clock)                # paper: no live flag needed
    monkeypatch.setattr(ap, "evaluate", _signal(entry=True))
    r.cycle()
    slot = r.state.slots["meanrev_4h_mk|ETH/USD"]
    assert slot.status == "entering" and fake.posts == []  # nothing ever reaches the exchange
    clock.t += timedelta(minutes=2)
    fake.minute_lows.append((int((clock.t.timestamp() - 60) * 1000), 3_990.0, 4_010.0))
    monkeypatch.setattr(ap, "evaluate", _signal())
    _bars(fake, clock)
    r.cycle()
    assert slot.status == "open" and slot.stop_order_id
    clock.t += timedelta(minutes=2)
    fake.minute_lows.append((int((clock.t.timestamp() - 60) * 1000), 3_870.0, 3_900.0))
    _bars(fake, clock)
    r.cycle()
    assert slot.status == "flat"
    t = json.loads((tmp_path / "trades.jsonl").read_text().splitlines()[0])
    assert t["reason"] == "stop" and fake.posts == []


def test_runner_never_sells_coins_you_hold_yourself(tmp_path, monkeypatch):
    clock = Clock(T0)
    fake = FakeBinanceUS(clock)
    fake.bal["BTC"] = 1.0                       # the owner's own BTC on the same account
    _bars(fake, clock)
    r, _ = _live_runner(tmp_path, clock, fake, max_trade_usd=25.0)
    monkeypatch.setattr(ap, "evaluate", _signal(entry=True))
    r.cycle()
    stop = fake.posts[1]
    assert float(stop["quantity"]) < 0.0003      # stop covers the bot's lot, not your 1 BTC
    monkeypatch.setattr(ap, "evaluate", _signal(exit_=True, reason="chandelier"))
    _tick(fake, clock, 60)
    r.cycle()
    assert r.state.slots["regime_1h|BTC/USD"].status == "flat"
    assert fake.bal["BTC"] >= 1.0 - 1e-9          # your coins are untouched

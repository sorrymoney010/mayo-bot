"""Alpaca gateway + paper runner, against an in-memory fake of the Alpaca API."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from dublin_bot import alpaca_paper as ap
from dublin_bot.alpaca_gateway import (
    LIVE_HOST, PAPER_HOST, AlpacaGateway, compact, live_locks_open, timeframe_str, to_alpaca,
)
from dublin_bot.alpaca_paper import SLEEVES, AlpacaPaperRunner, RunnerConfig, evaluate, report
from dublin_bot.backtest_core import add_indicators, meanrev_signals, regime_signals
from dublin_bot.config import Settings
from dublin_bot.engine import build_gateway
from dublin_bot.errors import SafetyLockError

T0 = datetime(2026, 11, 17, 12, 0, 30, tzinfo=timezone.utc)


class Resp:
    def __init__(self, code: int, body) -> None:
        self.status_code, self._body = code, body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeAlpaca:
    """Enough of Alpaca crypto to exercise the gateway and runner."""

    def __init__(self, clock, fee_bps: float = 25.0) -> None:
        self.clock = clock
        self.fee = fee_bps / 1e4
        self.prices = {"BTC/USD": 100_000.0, "ETH/USD": 4_000.0, "SOL/USD": 200.0}
        self.cash = 100_000.0
        self.pos: dict[str, float] = {}
        self.orders: dict[str, dict] = {}
        self.posts: list[dict] = []
        self.bars: dict[tuple[str, str], list[dict]] = {}
        self.fill_limits = True

    # helpers -------------------------------------------------------------
    def set_bars(self, sym: str, tf: str, closes: np.ndarray, step_min: int, end: datetime) -> None:
        n = len(closes)
        start = end - timedelta(minutes=step_min * n)
        rows = []
        for i, c in enumerate(closes):
            t = start + timedelta(minutes=step_min * i)
            o = closes[i - 1] if i else c
            rows.append({"t": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": o, "h": max(o, c) * 1.002,
                         "l": min(o, c) * 0.998, "c": c, "v": 10.0})
        self.bars[(sym, tf)] = rows

    def _fill(self, o: dict, px: float) -> None:
        qty = float(o["qty"])
        sym = o["symbol"]
        if o["side"] == "buy":
            self.cash -= qty * px
            self.pos[sym] = self.pos.get(sym, 0.0) + qty * (1 - self.fee)   # fee taken in coin
        else:
            self.pos[sym] = self.pos.get(sym, 0.0) - qty
            self.cash += qty * px * (1 - self.fee)
        o.update(status="filled", filled_qty=str(qty), filled_avg_price=str(px))

    def touch_limits(self) -> None:
        for o in self.orders.values():
            if o["status"] == "new" and o["type"] == "limit" and self.fill_limits:
                if self.prices[o["symbol"]] <= float(o["limit_price"]):
                    self._fill(o, float(o["limit_price"]))

    # http ----------------------------------------------------------------
    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        params = params or {}
        if "/v1beta3/crypto/us/bars" in url:
            sym = params["symbols"]
            rows = self.bars.get((sym, params["timeframe"]), [])
            start = pd.Timestamp(params["start"])
            rows = [r for r in rows if pd.Timestamp(r["t"]) >= start]
            if "page_token" not in params and len(rows) > 150:
                return Resp(200, {"bars": {sym: rows[:150]}, "next_page_token": "p2"})
            if params.get("page_token") == "p2":
                rows = rows[150:]
            return Resp(200, {"bars": {sym: rows}, "next_page_token": None})
        if "/latest/quotes" in url:
            p = self.prices[params["symbols"]]
            return Resp(200, {"quotes": {params["symbols"]: {"bp": p * 0.9999, "ap": p * 1.0001}}})
        if "/latest/trades" in url:
            return Resp(200, {"trades": {params["symbols"]: {"p": self.prices[params["symbols"]]}}})
        path = url.split(".markets", 1)[1]
        if path == "/v2/clock":
            return Resp(200, {"is_open": True})
        if path == "/v2/account":
            eq = self.cash + sum(q * self.prices[s] for s, q in self.pos.items())
            return Resp(200, {"equity": str(eq), "cash": str(self.cash),
                              "non_marginable_buying_power": str(self.cash), "status": "ACTIVE"})
        if path.startswith("/v2/assets/"):
            return Resp(200, {"min_order_size": "0.0001", "min_trade_increment": "0.0001",
                              "price_increment": "0.01", "tradable": True})
        if path.startswith("/v2/positions/"):
            sym = to_alpaca(path.rsplit("/", 1)[1])
            q = self.pos.get(sym, 0.0)
            if q <= 1e-12:
                return Resp(404, {"message": "position does not exist"})
            return Resp(200, {"symbol": compact(sym), "qty": str(q), "qty_available": str(q),
                              "asset_class": "crypto"})
        if path == "/v2/positions":
            return Resp(200, [{"symbol": compact(s), "qty": str(q), "asset_class": "crypto"}
                              for s, q in self.pos.items() if q > 1e-12])
        if path == "/v2/orders:by_client_order_id":
            for o in self.orders.values():
                if o["client_order_id"] == params["client_order_id"]:
                    return Resp(200, o)
            return Resp(404, {"message": "order not found"})
        if path == "/v2/orders" and method == "POST":
            self.posts.append(dict(json))
            oid = str(uuid.uuid4())
            o = {"id": oid, "status": "new", "filled_qty": "0", "filled_avg_price": None, **json}
            self.orders[oid] = o
            if json["type"] == "market":
                self._fill(o, self.prices[json["symbol"]])
            else:
                self.touch_limits()
            return Resp(200, o)
        if path == "/v2/orders" and method == "GET":
            return Resp(200, [o for o in self.orders.values() if o["status"] == "new"])
        if path.startswith("/v2/orders/"):
            oid = path.rsplit("/", 1)[1]
            if method == "DELETE":
                if self.orders[oid]["status"] == "new":
                    self.orders[oid]["status"] = "canceled"
                return Resp(204, {})
            return Resp(200, self.orders[oid])
        raise AssertionError(f"unexpected {method} {url}")


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def _settings(tmp_path, **kw) -> Settings:
    base = dict(_env_file=None, alpaca_api_key="k", alpaca_api_secret="s",
                audit_log_path=tmp_path / "audit.jsonl")
    base.update(kw)
    return Settings(**base)


def _gw(tmp_path, clock, fake, **kw):
    allow = kw.pop("allow", True)
    return AlpacaGateway(_settings(tmp_path, **kw), session=fake, allow_order_submission=allow,
                         clock=clock)


# ── gateway ──────────────────────────────────────────────

def test_symbols_and_timeframes():
    assert to_alpaca("BTCUSD") == "BTC/USD" and to_alpaca("sol/usd") == "SOL/USD"
    assert compact("ETH/USD") == "ETHUSD"
    assert timeframe_str(60) == "1Hour" and timeframe_str(240) == "4Hour"
    with pytest.raises(ValueError):
        timeframe_str(7)


def test_paper_is_default_and_live_needs_every_lock(tmp_path):
    clock = Clock(T0)
    fake = FakeAlpaca(clock)
    gw = _gw(tmp_path, clock, fake)
    assert gw.paper and gw.host == PAPER_HOST and gw.order_submission_enabled

    live = _gw(tmp_path, clock, fake, alpaca_paper=False)
    assert live.host == LIVE_HOST and not live.order_submission_enabled
    with pytest.raises(SafetyLockError):
        live.submit_order(symbol="BTC/USD", side="buy", order_type="market", qty=Decimal("0.001"),
                          client_order_id="x")
    assert fake.posts == []


def test_live_locks_helper_requires_acknowledgement(tmp_path):
    s = _settings(tmp_path, allow_live_trading=True, paper_trading=False, dry_run=False,
                  live_risk_acknowledgement="I_ACCEPT_LIVE_TRADING_RISK")
    assert live_locks_open(s)
    # The acknowledgement is required by Settings itself whenever the locks open,
    # so "locks open without ack" can't even be constructed; paper settings are closed.
    assert not live_locks_open(_settings(tmp_path))


def test_gateway_without_permission_refuses(tmp_path):
    clock = Clock(T0)
    fake = FakeAlpaca(clock)
    gw = _gw(tmp_path, clock, fake, allow=False)
    with pytest.raises(SafetyLockError):
        gw.submit_order(symbol="BTC/USD", side="buy", order_type="market", qty=Decimal("0.001"),
                        client_order_id="x")
    assert gw.buy_notional(10.0, userref=1).startswith("alpaca-dry-")
    assert fake.posts == []


def test_build_gateway_alpaca_paper_settings_cannot_submit(tmp_path):
    gw = build_gateway(_settings(tmp_path, broker="alpaca"))
    assert isinstance(gw, AlpacaGateway) and not gw.order_submission_enabled


def test_submit_is_idempotent_on_client_id(tmp_path):
    clock = Clock(T0)
    fake = FakeAlpaca(clock)
    gw = _gw(tmp_path, clock, fake)
    a = gw.submit_order(symbol="BTC/USD", side="buy", order_type="market", qty=Decimal("0.001"),
                        client_order_id="same")
    b = gw.submit_order(symbol="BTC/USD", side="buy", order_type="market", qty=Decimal("0.001"),
                        client_order_id="same")
    assert a["order_id"] == b["order_id"] and len(fake.posts) == 1


def test_fetch_bars_pages_and_drops_forming_bar(tmp_path):
    clock = Clock(T0)
    fake = FakeAlpaca(clock)
    # Last row starts at T0 - 60m + ... ; build so the final bar is still forming.
    end = T0.replace(second=0) + timedelta(minutes=30)
    fake.set_bars("BTC/USD", "1Hour", np.linspace(100, 120, 300), 60, end)
    gw = _gw(tmp_path, clock, fake)
    df = gw.fetch_bars("BTC/USD", 60, bars=250)
    assert len(df) == 250
    assert df["time"].is_monotonic_increasing
    assert df["time"].iloc[-1] + 3600 <= T0.timestamp()   # every returned bar is closed


def test_asset_rounding():
    from dublin_bot.alpaca_gateway import AssetMeta
    a = AssetMeta("BTC/USD", Decimal("0.0001"), Decimal("0.0001"), Decimal("1"), True)
    assert a.floor_qty(0.00019) == Decimal("0.0001")
    assert a.floor_qty(0.00009) == 0
    assert a.floor_price(100123.9) == Decimal("100123")


# ── signal parity with the backtester ───────────────────

def _walk(n: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.012, n)))
    o = np.concatenate([[c[0]], c[:-1]])
    return pd.DataFrame({"time": np.arange(n) * 3600.0, "open": o, "high": np.maximum(o, c) * 1.004,
                         "low": np.minimum(o, c) * 0.996, "close": c, "volume": rng.uniform(5, 15, n)})


@pytest.mark.parametrize("sleeve", list(SLEEVES))
def test_evaluate_matches_backtest_signals(sleeve):
    spec = SLEEVES[sleeve]
    raw = _walk(700, 7)
    d = add_indicators(raw)
    full = regime_signals(d, spec.params) if spec.family == "regime" else meanrev_signals(d, spec.params)
    fires = set((np.flatnonzero(full["entry"][300:]) + 300).tolist())
    assert fires, "fixture must contain real entry signals"
    seen = 0
    for i in sorted(fires | set(range(300, 700, 7))):
        sig = evaluate(spec, raw.iloc[: i + 1].reset_index(drop=True))
        assert sig["entry"] == bool(full["entry"][i]), i
        assert sig["bar"] == raw["time"].iloc[i]
        seen += sig["entry"]
    assert seen == len(fires)


# ── runner ───────────────────────────────────────────────

def _runner(tmp_path, clock, fake, sleeves=("meanrev_4h_mk",), symbols=("BTC/USD",), **cfg):
    gw = _gw(tmp_path, clock, fake)
    rc = RunnerConfig(state_path=tmp_path / "state.json", journal_path=tmp_path / "trades.jsonl",
                      events_path=tmp_path / "events.jsonl", sleeves=sleeves, symbols=symbols,
                      budget_usd=cfg.pop("budget_usd", 1000.0), **cfg)
    return AlpacaPaperRunner(gw, rc, clock=clock)


def _seed_bars(fake, clock, tf_min=240, n=300):
    end = clock().replace(second=0)          # last bar just closed at "now"
    tf = timeframe_str(tf_min)
    for sym, px in fake.prices.items():
        fake.set_bars(sym, tf, np.full(n, px), tf_min, end)


def _signal(entry=False, exit_=False, reason=""):
    def f(spec, raw):
        return {"bar": float(raw["time"].iloc[-1]), "close": float(raw["close"].iloc[-1]),
                "warm": True, "entry": entry, "exit": exit_, "exit_reason": reason}
    return f


def _advance(fake, clock, minutes: int, tf_min=240):
    clock.t = clock.t + timedelta(minutes=minutes)
    _seed_bars(fake, clock, tf_min)


def test_runner_refuses_live_gateway(tmp_path):
    clock = Clock(T0)
    gw = _gw(tmp_path, clock, FakeAlpaca(clock), alpaca_paper=False)
    with pytest.raises(RuntimeError):
        AlpacaPaperRunner(gw, RunnerConfig(state_path=tmp_path / "s.json"))


def test_limit_entry_fills_then_stop_exits_and_journals(tmp_path, monkeypatch):
    clock = Clock(T0)
    fake = FakeAlpaca(clock)
    _seed_bars(fake, clock)
    r = _runner(tmp_path, clock, fake)
    monkeypatch.setattr(ap, "evaluate", _signal(entry=True))
    fake.fill_limits = False
    r.cycle()
    slot = r.state.slots["meanrev_4h_mk|BTC/USD"]
    assert slot.status == "entering"
    post = fake.posts[-1]
    assert post["type"] == "limit" and float(post["limit_price"]) == pytest.approx(99_900.0)
    # budget 1000, 1 slot: min(1000*0.01/0.03, 1000) = 333.33 -> qty 0.0033 at 99,900
    assert Decimal(post["qty"]) == Decimal("0.0033")

    # Price dips to the limit: fills.
    fake.fill_limits = True
    fake.prices["BTC/USD"] = 99_800.0
    fake.touch_limits()
    monkeypatch.setattr(ap, "evaluate", _signal())
    _advance(fake, clock, 1)
    r.cycle()
    assert slot.status == "open" and slot.entry_px == pytest.approx(99_900.0)
    assert slot.stop_px == pytest.approx(99_900.0 * 0.97)

    # Price falls through the 3% stop: market sell of what Alpaca actually holds.
    fake.prices["BTC/USD"] = 96_000.0
    _advance(fake, clock, 1)
    r.cycle()
    assert slot.status == "flat"
    sell = fake.posts[-1]
    assert sell["side"] == "sell" and sell["type"] == "market"
    assert Decimal(sell["qty"]) <= Decimal("0.0033")     # fee was taken in coin
    trades = [json.loads(x) for x in (tmp_path / "trades.jsonl").read_text().splitlines()]
    assert len(trades) == 1 and trades[0]["reason"] == "stop" and trades[0]["net_bps"] < -300
    rep = {x["sleeve"]: x for x in report(tmp_path / "trades.jsonl")}
    assert rep["meanrev_4h_mk"]["trades"] == 1 and not rep["meanrev_4h_mk"]["passes_gate"]


def test_unfilled_limit_is_cancelled_after_one_bar(tmp_path, monkeypatch):
    clock = Clock(T0)
    fake = FakeAlpaca(clock)
    fake.fill_limits = False
    _seed_bars(fake, clock)
    r = _runner(tmp_path, clock, fake)
    monkeypatch.setattr(ap, "evaluate", _signal(entry=True))
    r.cycle()
    slot = r.state.slots["meanrev_4h_mk|BTC/USD"]
    oid = slot.entry_order_id
    monkeypatch.setattr(ap, "evaluate", _signal())
    _advance(fake, clock, 239)
    r.cycle()
    assert slot.status == "entering"            # still inside its one-bar window
    _advance(fake, clock, 2)
    r.cycle()
    assert slot.status == "flat" and fake.orders[oid]["status"] == "canceled"
    assert not (tmp_path / "trades.jsonl").exists()


def test_market_entry_and_signal_exit(tmp_path, monkeypatch):
    clock = Clock(T0)
    fake = FakeAlpaca(clock)
    _seed_bars(fake, clock, 60)
    r = _runner(tmp_path, clock, fake, sleeves=("regime_1h",), symbols=("ETH/USD",))
    monkeypatch.setattr(ap, "evaluate", _signal(entry=True))
    r.cycle()
    slot = r.state.slots["regime_1h|ETH/USD"]
    assert slot.status == "open" and fake.posts[-1]["type"] == "market"
    fake.prices["ETH/USD"] = 4_200.0
    monkeypatch.setattr(ap, "evaluate", _signal(exit_=True, reason="chandelier"))
    _advance(fake, clock, 60, 60)
    r.cycle()
    assert slot.status == "flat"
    t = json.loads((tmp_path / "trades.jsonl").read_text().splitlines()[0])
    assert t["reason"] == "chandelier" and t["gross_bps"] == pytest.approx(500.0, abs=1)
    assert t["cost_bps"] == pytest.approx(50.0)   # taker in, taker out at 25 bps


def test_stale_signal_never_enters_but_state_survives_restart(tmp_path, monkeypatch):
    clock = Clock(T0)
    fake = FakeAlpaca(clock)
    _seed_bars(fake, clock)
    clock.t = T0 + timedelta(minutes=90)        # last closed bar closed 90 min ago
    r = _runner(tmp_path, clock, fake)
    monkeypatch.setattr(ap, "evaluate", _signal(entry=True))
    r.cycle()
    assert fake.posts == []
    r2 = _runner(tmp_path, clock, fake)
    assert r2.state.slots["meanrev_4h_mk|BTC/USD"].last_bar > 0


def test_daily_entry_cap_and_restart_does_not_double_order(tmp_path, monkeypatch):
    clock = Clock(T0)
    fake = FakeAlpaca(clock)
    fake.fill_limits = False
    _seed_bars(fake, clock)
    r = _runner(tmp_path, clock, fake, symbols=("BTC/USD", "ETH/USD", "SOL/USD"),
                max_entries_per_day=2)
    monkeypatch.setattr(ap, "evaluate", _signal(entry=True))
    r.cycle()
    assert len(fake.posts) == 2
    # Crash before state was saved: a fresh runner on the same bar re-derives the
    # same client ids, so Alpaca returns the existing orders instead of new ones.
    (tmp_path / "state.json").unlink()
    r2 = _runner(tmp_path, clock, fake, symbols=("BTC/USD", "ETH/USD", "SOL/USD"),
                 max_entries_per_day=2)
    r2.cycle()
    assert len(fake.posts) == 2


def test_slots_never_overcommit_cash(tmp_path, monkeypatch):
    clock = Clock(T0)
    fake = FakeAlpaca(clock)
    fake.fill_limits = False
    _seed_bars(fake, clock)
    r = _runner(tmp_path, clock, fake, sleeves=tuple(SLEEVES),
                symbols=("BTC/USD", "ETH/USD", "SOL/USD"), budget_usd=900.0, max_entries_per_day=9)
    monkeypatch.setattr(ap, "evaluate", _signal(entry=True))
    _seed_bars(fake, clock, 60)
    _seed_bars(fake, clock, 240)
    r.cycle()
    total = sum(float(p["qty"]) * (float(p.get("limit_price") or fake.prices[p["symbol"]]))
                for p in fake.posts)
    assert total <= 900.0 + 1e-6

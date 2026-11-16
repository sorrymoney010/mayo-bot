"""TREND-6 paper runner: signals, sizing, next-open execution / no look-ahead, band + min notional,
fee books, round trips, kills, Binance global warm-up across the Binance.US gap, idempotency,
catch-up, no order endpoints. No network."""
from __future__ import annotations

import inspect
import json

import numpy as np
import pandas as pd

from dublin_bot import trend6 as T

DAY0 = pd.Timestamp("2025-01-01", tz="UTC")


def daily(closes, start=DAY0, opens=None):
    closes = np.asarray(closes, float)
    idx = pd.date_range(start, periods=len(closes), freq="D", tz="UTC")
    o = np.r_[closes[0], closes[:-1]] if opens is None else np.asarray(opens, float)
    ct = (idx.astype("int64") // 10**6) + 86_399_999
    return pd.DataFrame({"o": o, "h": np.maximum(o, closes), "l": np.minimum(o, closes), "c": closes,
                         "ct": ct.astype(float)}, index=idx)


def runner(tmp, data, now, warm=None):
    clock = {"now": pd.Timestamp(now)}

    def fb(c):
        d = data[c]
        return d[d.index <= clock["now"].normalize()]

    r = T.Trend6Paper(tmp, now=lambda: clock["now"], fetch_bus=fb,
                      fetch_warm=(warm or (lambda c, d: None)), fetch_hour_open=lambda c, d: None)
    return r, clock


def test_signals_all_on_and_off():
    up = pd.Series(np.linspace(100, 300, 260), index=pd.date_range(DAY0, periods=260, freq="D", tz="UTC"))
    s = T.signals_at(up, up.index[-1])
    assert s["yes"] == 6 and s["weight"] == 0.5
    dn = pd.Series(up.values[::-1], index=up.index)
    s = T.signals_at(dn, dn.index[-1])
    assert s["yes"] == 0 and s["weight"] == 0.0


def test_signal_definitions_exact():
    c = pd.Series(np.r_[np.full(230, 100.0), np.linspace(100, 90, 29), 101.0],
                  index=pd.date_range(DAY0, periods=260, freq="D", tz="UTC"))
    s = T.signals_at(c, c.index[-1])["signals"]
    last = c.iloc[-1]
    assert s["sma50"] == (last > c.iloc[-50:].mean())
    assert s["sma200"] == (last > c.iloc[-200:].mean())
    assert s["ret7"] == (last / c.iloc[-8] - 1 > 0)
    assert s["ret28"] == (last / c.iloc[-29] - 1 > 0)


def test_sizing_steps():
    assert [T.target_weight(k) for k in range(7)] == [0.5 * k / 6 for k in range(7)]


def test_insufficient_history_returns_none():
    c = pd.Series(np.ones(150), index=pd.date_range(DAY0, periods=150, freq="D", tz="UTC"))
    assert T.signals_at(c, c.index[-1]) is None


def test_no_lookahead_future_data_does_not_change_decision():
    rng = np.random.default_rng(3)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.03, 400)))
    s = pd.Series(c, index=pd.date_range(DAY0, periods=400, freq="D", tz="UTC"))
    d = s.index[300]
    a = T.signals_at(s, d)
    s2 = s.copy()
    s2.iloc[301:] = s2.iloc[301:] * 10
    assert T.signals_at(s2, d) == a


def test_executes_at_next_open_not_close(tmp_path):
    n = 260
    closes = np.linspace(100, 300, n)
    opens = np.r_[closes[0], closes[:-1]].copy()
    opens[-1] = 777.0  # the open of the day after the decision day differs from the close
    data = {c: daily(closes, opens=opens) for c in T.COINS}
    last = data["BTC"].index[-1]
    r, _ = runner(tmp_path, data, last + pd.Timedelta("2h"))
    st = r.run_cycle()
    assert st["ok"] and st["targets"] == {"BTC": 0.5, "ETH": 0.5}
    fills = [json.loads(x) for x in (tmp_path / "fills.jsonl").read_text().splitlines()]
    assert fills and all(f["price"] == 777.0 for f in fills)
    dec = st["last_decision"]
    assert dec["decision_day"] == str((last - pd.Timedelta("1D")).date())
    assert dec["coins"]["BTC"]["close"] == closes[-2]


def test_waits_when_next_open_missing(tmp_path):
    data = {c: daily(np.linspace(100, 300, 260)) for c in T.COINS}
    last = data["BTC"].index[-1]
    # 'now' just after the last bar CLOSED, but the bar for the next day is not in the feed
    r, _ = runner(tmp_path, data, last + pd.Timedelta("1D") + pd.Timedelta("1min"))
    st = r.run_cycle()
    assert st["ok"] and st["new_decisions_this_cycle"] == []
    assert not (tmp_path / "fills.jsonl").exists()


def test_idempotent_and_catch_up(tmp_path):
    rng = np.random.default_rng(5)
    closes = 100 * np.exp(np.cumsum(rng.normal(0.002, 0.03, 330)))
    data = {c: daily(closes * (1 if c == "BTC" else 0.05)) for c in T.COINS}
    r, clock = runner(tmp_path, data, data["BTC"].index[260] + pd.Timedelta("1h"))
    r.run_cycle()
    s1 = (tmp_path / "state.json").read_text()
    r.run_cycle()
    assert (tmp_path / "state.json").read_text() == s1  # same day: nothing new
    clock["now"] = data["BTC"].index[300] + pd.Timedelta("1h")  # 40 days later in one go
    st = r.run_cycle()
    assert len(st["new_decisions_this_cycle"]) == 40
    days = [json.loads(x)["decision_day"] for x in (tmp_path / "decisions.jsonl").read_text().splitlines()]
    assert days == sorted(set(days))  # each day decided exactly once, in order


def test_band_only_trades_on_target_change(tmp_path):
    closes = np.linspace(100, 300, 280)
    data = {c: daily(closes) for c in T.COINS}
    r, clock = runner(tmp_path, data, data["BTC"].index[260] + pd.Timedelta("1h"))
    r.run_cycle()
    n1 = len((tmp_path / "fills.jsonl").read_text().splitlines())
    clock["now"] = data["BTC"].index[279] + pd.Timedelta("1h")
    r.run_cycle()  # target stays 0.5 while prices drift up: no rebalancing
    assert len((tmp_path / "fills.jsonl").read_text().splitlines()) == n1


def test_min_notional_skip_and_fee_books():
    book = T._new_book()
    px = {"BTC": 100.0, "ETH": 10.0}
    r = T.apply_target(book, "BTC", 0.5, px, 0.0004)
    assert r["status"] == "filled" and abs(r["fee"] - 50 * 0.0004) < 1e-12
    r2 = T.apply_target(book, "BTC", 0.505, px, 0.0004)  # ~$0.5 change < $1 minimum
    assert r2["status"] == "skipped_min_notional"
    r3 = T.apply_target(book, "BTC", 0.0, {"BTC": 110.0, "ETH": 10.0}, 0.0004)
    assert r3["status"] == "filled" and book["units"]["BTC"] == 0.0
    assert len(book["closed"]) == 1
    exp = 50 * 1.1 * (1 - 0.0004) - 50 * (1 + 0.0004)
    assert abs(book["closed"][0] - exp) < 1e-6
    assert T.SIDE_COST["binanceus"] < T.SIDE_COST["binanceus10"] < T.SIDE_COST["ref_kraken80"]
    assert T.PRIMARY == "binanceus" and set(T.REFERENCE_ONLY) == {"ref_kraken80", "ref_kraken160"}


def test_buy_never_overdraws_cash():
    book = T._new_book()
    px = {"BTC": 100.0, "ETH": 10.0}
    T.apply_target(book, "BTC", 0.5, px, 0.008)
    T.apply_target(book, "ETH", 0.5, px, 0.008)
    assert book["cash"] >= -1e-9


def test_warmup_from_binance_global_across_gap(tmp_path):
    rng = np.random.default_rng(9)
    closes = 100 * np.exp(np.cumsum(rng.normal(0.001, 0.02, 500)))
    full = daily(closes)
    bus = full.iloc[np.r_[0:200, 380:500]]  # Binance.US hole: only 120 contiguous days at the end
    warm = full[["o", "h", "l", "c"]] * 1.0005  # Binance global prices
    data = {c: bus for c in T.COINS}
    r, _ = runner(tmp_path, data, bus.index[-1] + pd.Timedelta("1h"), warm=lambda c, d: warm)
    st = r.run_cycle()
    assert st["ok"]
    info = st["last_decision"]["coins"]["BTC"]["data"]
    assert info["source"] == "binanceus+binance_global_warmup"
    assert info["binanceus_days"] == 119 and info["binance_global_days"] > 100
    assert st["last_decision"]["coins"]["BTC"]["exec_venue"] == "binanceus"


def test_insufficient_without_warmup_is_error_not_trade(tmp_path):
    full = daily(np.linspace(100, 200, 300))
    bus = full.iloc[np.r_[0:100, 200:300]]
    r, _ = runner(tmp_path, {c: bus for c in T.COINS}, bus.index[-1] + pd.Timedelta("1h"))
    st = r.run_cycle()
    assert not st["ok"] and not (tmp_path / "fills.jsonl").exists()


def _crash_world():
    up = np.linspace(100, 300, 260)
    crash = 300 * np.cumprod(np.full(40, 0.97))
    rebound = np.linspace(crash[-1], 600, 120)
    closes = np.r_[up, crash, rebound]
    return {c: daily(closes) for c in T.COINS}


def test_drawdown_kill_stops_buys(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "KILL_DD", 0.05)
    data = _crash_world()
    r, clock = runner(tmp_path, data, data["BTC"].index[259] + pd.Timedelta("1h"))
    r.run_cycle()
    clock["now"] = data["BTC"].index[-1] + pd.Timedelta("1h")
    st = r.run_cycle()
    assert st["killed"]["rule"].startswith("drawdown")
    killed_at = pd.Timestamp(st["killed"]["at"])
    fills = [json.loads(x) for x in (tmp_path / "fills.jsonl").read_text().splitlines()]
    assert not [f for f in fills if f["side"] == "buy" and pd.Timestamp(f["exec_at"]) > killed_at]
    skipped = [json.loads(x) for x in (tmp_path / "skipped.jsonl").read_text().splitlines()]
    assert any(x["status"] == "skipped_killed" for x in skipped)  # the rebound buys were refused


def test_pf_kill_rule(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "KILL_MIN_TRADES", 1)
    monkeypatch.setattr(T, "KILL_DD", 0.99)
    data = _crash_world()
    r, clock = runner(tmp_path, data, data["BTC"].index[259] + pd.Timedelta("1h"))
    r.run_cycle()
    clock["now"] = data["BTC"].index[310] + pd.Timedelta("1h")
    st = r.run_cycle()
    closed = json.loads((tmp_path / "state.json").read_text())["books"]["binanceus"]["closed"]
    assert closed and T.profit_factor(closed) < T.KILL_MIN_PF
    assert st["killed"]["rule"].startswith("PF")


def test_status_fields_and_paper_only(tmp_path):
    data = {c: daily(np.linspace(300, 100, 260)) for c in T.COINS}
    r, _ = runner(tmp_path, data, data["BTC"].index[-1] + pd.Timedelta("1h"))
    st = r.run_cycle()
    assert st["mode"] == "PAPER" and st["orders_placed"] == 0
    assert st["equity_primary"] == 100.0 and st["cash_pct_primary"] == 1.0
    assert st["venue"].startswith("binanceus")


def test_no_order_endpoints_or_keys():
    import scripts.trend6_paper as S  # noqa: F401
    src = inspect.getsource(T) + open(S.__file__).read()
    for bad in ("/api/v3/order", "signature", "X-MBX-APIKEY", "API_SECRET", "dotenv", "ALLOW_LIVE_TRADING =",
                "coinbase.com", "kraken.com"):
        assert bad not in src, bad

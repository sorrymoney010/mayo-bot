import numpy as np, pandas as pd
from dublin_bot import intraday_sleeve as m


def _bars(n=400, tf=15, start=100.0, drift=0.0):
    idx = pd.date_range("2026-10-01", periods=n, freq=f"{tf}min", tz="UTC")
    c = start + np.cumsum(np.full(n, drift))
    return pd.DataFrame({"open": c, "high": c + 0.5, "low": c - 0.5, "close": c, "volume": 10.0}, index=idx)


def test_breakout_signal_spec():
    b = _bars(drift=0.05)
    b.iloc[-1, b.columns.get_loc("close")] += 3; b.iloc[-1, b.columns.get_loc("high")] += 3
    b.iloc[-1, b.columns.get_loc("volume")] = 30
    f = m.signal_frame(b)
    row = f.iloc[-1]
    assert row.ema20 > row.ema50 and row.close > row.hi20 and row.vr >= 1.1


def test_cycle_paper_only_limit_fill_and_ladder(tmp_path, monkeypatch):
    monkeypatch.setenv("INTRADAY_SLEEVE_ENABLED", "true")
    monkeypatch.setenv("INTRADAY_TFS", "15")
    monkeypatch.setenv("INTRADAY_VARIANTS", "user_breakout")
    now = [pd.Timestamp("2026-10-05 04:00", tz="UTC").timestamp()]
    s = m.IntradaySleeve(root=tmp_path, now_fn=lambda: now[0])
    sig = _bars(drift=0.0)
    monkeypatch.setattr(s, "bars", lambda sym, tf: sig)
    monkeypatch.setattr(m, "signal_frame", lambda b: pd.DataFrame(
        {"close": [100.0], "atr": [1.0], "rsi": [55], "vr": [2], "ema20": [1], "ema50": [0], "ema200": [0],
         "hi20": [99], "user_breakout": [True], "user_emacross": [False], "pullback_trend": [False]},
        index=[pd.Timestamp("2026-10-05 03:45", tz="UTC")]).reindex(
        [pd.Timestamp("2026-10-05 03:45", tz="UTC")] * 1).iloc[[0] * 230].set_axis(
        [pd.Timestamp("2026-10-05 03:45", tz="UTC")] * 230))
    path = {"p": pd.DataFrame(columns=["open", "high", "low", "close"])}
    monkeypatch.setattr(s, "minute_path", lambda sym, since: path["p"])
    out = s.run_cycle()
    assert any(a["event"] == "post_only_limit" for a in out["actions"])
    lane = next(iter(s.state["pending"]))
    # price trades through the limit, then rallies through TP1..TP3 (1.5 ATR risk)
    t = pd.date_range(pd.Timestamp(now[0], unit="s", tz="UTC"), periods=3, freq="1min")
    path["p"] = pd.DataFrame({"open": [100, 100.5, 102], "high": [100.2, 102, 105],
                              "low": [99.8, 100.1, 101.9], "close": [100, 101.8, 104.9]}, index=t)
    now[0] += 240
    out = s.run_cycle()
    ev = [a["event"] for a in out["actions"]]
    assert "maker_fill" in ev and "tp1" in ev and "tp3" in ev
    assert lane not in s.state["positions"]
    assert (tmp_path / "logs/intraday/trades.jsonl").exists()


def test_disabled_without_flag(tmp_path, monkeypatch):
    monkeypatch.delenv("INTRADAY_SLEEVE_ENABLED", raising=False)
    assert m.IntradaySleeve(root=tmp_path).run_cycle()["active"] is False


def test_refuses_when_not_paper(tmp_path, monkeypatch):
    monkeypatch.setenv("INTRADAY_SLEEVE_ENABLED", "true")
    monkeypatch.setenv("ALLOW_LIVE_TRADING", "true")
    assert m.sleeve_active()[0] is False


def _fire_state(close=100.0, atr=1.2):
    return {"bar": "2026-10-20T11:45:00+00:00", "close": close, "atr": atr, "fire": True, "h_up": True,
            "h_strong": True, "atr_x": True, "m_rsi": 60.0, "vr": 1.5, "tgt_pct": 0.09}, "FIRE"


def test_mtf_v2_fewer_longer_limits_and_ladder(tmp_path, monkeypatch):
    monkeypatch.setenv("INTRADAY_SLEEVE_ENABLED", "true")
    monkeypatch.setenv("INTRADAY_VARIANTS", "mtf_synth")
    now = [pd.Timestamp("2026-10-20 12:00", tz="UTC").timestamp()]
    s = m.IntradaySleeve(root=tmp_path, now_fn=lambda: now[0])
    monkeypatch.setattr(s, "mtf_state", lambda sym: _fire_state())
    monkeypatch.setattr(s, "bars", lambda sym, tf: pd.DataFrame())
    path = {"p": pd.DataFrame(columns=["open", "high", "low", "close"])}
    monkeypatch.setattr(s, "minute_path", lambda sym, since: path["p"])
    out = s.run_cycle()
    # max 2 open / 2 entries per Chicago day across all pairs (3 pairs fire)
    assert sum(a["event"] == "post_only_limit" for a in out["actions"]) == 2
    p = next(iter(s.state["pending"].values()))
    assert p["risk"] == 2.5 * 1.2 and p["ladder"] == [3.0] and p["time_stop_h"] == 48.0
    # fill, then rally through 3R (=109): half off at 3R maker, stop to entry+0.2%
    t = pd.date_range(pd.Timestamp(now[0], unit="s", tz="UTC"), periods=2, freq="1min")
    path["p"] = pd.DataFrame({"open": [100, 101], "high": [100.1, 109.5], "low": [99.9, 100.9],
                              "close": [100, 109.2]}, index=t)
    now[0] += 180
    out = s.run_cycle()
    ev = [a["event"] for a in out["actions"]]
    assert ev.count("maker_fill") == 2 and ev.count("tp1") == 2
    pos = next(iter(s.state["positions"].values()))
    assert abs(pos["qty"] - pos["qty0"] / 2) < 1e-9 and abs(pos["stop"] - 100.2) < 1e-9
    # entries cap: no new limits same day even after slots free up
    assert not s.state["pending"]


def test_mtf_v2_cooldown_after_loss(tmp_path, monkeypatch):
    monkeypatch.setenv("INTRADAY_SLEEVE_ENABLED", "true")
    monkeypatch.setenv("INTRADAY_VARIANTS", "mtf_synth")
    now = [pd.Timestamp("2026-10-20 12:00", tz="UTC").timestamp()]
    s = m.IntradaySleeve(root=tmp_path, now_fn=lambda: now[0])
    pos = {"lane": "mtf_synth|15|BTC/USD", "variant": "mtf_synth", "tf": 15, "symbol": "BTC/USD", "entry": 100.0,
           "qty": 0.0, "qty0": 1.0, "hit": 0, "opened": now[0] - 3600, "realized": -3.0}
    s.state["positions"][pos["lane"]] = pos
    s._close_if_done(pos["lane"], pos)
    assert s.state["cooldown"]["BTC/USD"] == now[0] + 12 * 3600


def test_mtf_v2_skips_small_target(tmp_path, monkeypatch):
    b = pd.Series([1.0])
    st = {"close": 100.0, "atr": 0.2}
    assert m.MTF["tp_r"] * m.MTF["stop_atr"] * st["atr"] / st["close"] < m.MTF["min_target"]

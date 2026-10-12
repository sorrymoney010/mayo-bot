import json
import numpy as np
import pandas as pd
import dublin_bot.alt_regime_sleeve as m
from dublin_bot.config import Settings

TF = 240 * 60


def _bars(target):
    n = 300
    t = (target - np.arange(n)[::-1] * TF).astype("int64")
    c = np.linspace(1.0, 1.2, n)
    df = pd.DataFrame({"time": t, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1.0})
    df.index = pd.to_datetime(df["time"], unit="s", utc=True)
    return df


def test_paper_entry_caps_stop_and_isolation(tmp_path, monkeypatch):
    s = Settings()
    assert s.paper_trading and s.dry_run and not s.allow_live_trading
    now = 1_791_800_000.0
    sl = m.AltRegimeSleeve(s, base_dir=tmp_path, now_fn=lambda: now,
                           ticker=lambda sym: {"bid": 1.0, "ask": 1.001, "last": 1.0})
    target = sl._bar_target(now)
    sl.fetch = lambda sym, tf: _bars(target)

    def fake(d, p):
        n = len(d)
        e = np.zeros(n, bool); e[-1] = True
        return {"entry": e, "adx_on": np.ones(n, bool), "chandelier_exit": np.zeros(n, bool)}
    monkeypatch.setattr(m, "regime_signals", fake)
    r = sl.run_cycle()
    assert r["errors"] == []
    st = json.loads((tmp_path / "alt_regime_sleeve.json").read_text())
    assert len(st["positions"]) == 3  # max_open cap with 8 signals
    book = json.loads((tmp_path / "paper_portfolio.json").read_text())
    for p in book["positions"]:
        assert abs(p["quantity"] * p["entry_price"] - s.strategy_equity_usd * 0.05) < s.strategy_equity_usd * 0.01
    # fast watcher closes on the 3% stop in the sub-book only
    w = m.AltExitWatcher(s, base_dir=tmp_path, ticker=lambda sym: {"last": 0.9})
    monkeypatch.setattr(w, "allowed", lambda: (True, "ok"))
    sym = sorted(st["positions"])[0]
    row = w.evaluate(sym, 0.95, now + 60)
    assert row and row["reason"] == "stop" and row["realized"] < 0
    r2 = sl.run_cycle()  # sleeve sees the watcher close
    assert sym not in r2["open"]
    assert not (tmp_path / "paper_bot_positions.json").exists() or sym not in json.loads(
        (tmp_path / "paper_bot_positions.json").read_text())


def test_refuses_without_paper_locks(tmp_path):
    s = Settings().model_copy(update={"allow_live_trading": True})
    assert m.AltRegimeSleeve(s, base_dir=tmp_path).run_cycle()["active"] is False

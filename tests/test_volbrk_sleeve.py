"""volbrk_1d paper sleeve: research-exact signal + 10-bar time stop."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from dublin_bot.exit_watcher import sleeve_strategy_key, spot_exit_levels
from dublin_bot.sleeve_registry import SleeveRegistry
from dublin_bot.volbrk_sleeve import (DAY, SLEEVE, VolBrkSleeve, bars_held, research_atr,
                                      sleeve_active, volbrk_signal, volbrk_state_series)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_meanrev_sleeve import FakeGateway, _settings, _tick

AUDIT = Path("/workspace/mayo-audit/v2_20261023")
L0 = 1_790_000_000 // DAY * DAY  # a UTC midnight


# verbatim copy of the research reference (search.py atr + sig 'volbrk')
def _ref_atr(d, n=14):
    tr = pd.concat([d.high - d.low, (d.high - d.close.shift()).abs(), (d.low - d.close.shift()).abs()], axis=1).max(1)
    return tr.rolling(n).mean()


def _ref_sig(d, k, h):
    c = d.close
    a = _ref_atr(d); e = c > c.rolling(100).mean()
    st = np.zeros(len(c), bool); left = 0
    for i in range(1, len(c)):
        if left > 0: left -= 1
        elif c.iloc[i] > c.iloc[i - 1] + k * a.iloc[i - 1] and e.iloc[i]: left = h
        st[i] = left > 0
    return pd.Series(st, c.index)


def _rw(n=900, seed=7):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0.001, 0.035, n)))
    o = np.r_[c[0], c[:-1]]
    hi = np.maximum(o, c) * (1 + rng.uniform(0, 0.02, n))
    lo = np.minimum(o, c) * (1 - rng.uniform(0, 0.02, n))
    idx = pd.to_datetime(L0 - (n - 1 - np.arange(n)) * DAY, unit="s", utc=True)
    return pd.DataFrame({"open": o, "high": hi, "low": lo, "close": c, "volume": 1e3}, index=idx)


def _frames():
    out = [_rw(seed=s) for s in (1, 2, 3)]
    for sym in ("BTCUSD", "ETHUSD", "SOLUSD"):
        f = AUDIT / f"kraken_{sym}_1440m.csv"
        if f.exists():
            d = pd.read_csv(f)
            d.index = pd.to_datetime(d.time, unit="s", utc=True)
            out.append(d[["open", "high", "low", "close"]].astype(float))
    return out


@pytest.mark.parametrize("k,h", [(1.0, 10), (1.25, 10), (1.5, 10), (1.5, 20)])
def test_state_series_matches_research_exactly(k, h):
    for d in _frames():
        assert (volbrk_state_series(d, k, h).values == _ref_sig(d, k, h).values).all()
    pd.testing.assert_series_equal(research_atr(_rw()), _ref_atr(_rw()))


def test_last_bar_signal_matches_research_trigger():
    d = _rw(seed=11)
    c, a, sma = d.close, _ref_atr(d), d.close.rolling(100).mean()
    fires = 0
    for i in range(101, len(d)):
        want = bool(c.iloc[i] > c.iloc[i - 1] + 1.25 * a.iloc[i - 1] and c.iloc[i] > sma.iloc[i])
        got = volbrk_signal(d.iloc[: i + 1], 1.25)["fire"]
        assert got == want, i
        fires += want
    assert fires > 0


def test_signal_warmup_and_trend_filter():
    assert not volbrk_signal(_rw(n=50))["fire"]
    d = _breakout_bars()
    assert volbrk_signal(d)["fire"]
    down = d.copy()
    down[["open", "high", "low", "close"]] = down[["open", "high", "low", "close"]].iloc[::-1].values
    down.iloc[-1] = d.iloc[-1] * 0.5  # jump but far below SMA100
    assert not volbrk_signal(down)["fire"]


def test_bars_held_counts_from_entry_bar():
    d = _rw(n=30)
    assert bars_held(d, d.index[-10].timestamp()) == 10
    assert bars_held(d, d.index[-1].timestamp() + DAY) == 0


# ── sleeve ─────────────────────────────────────────────────────
def _breakout_bars(n=150, last_open=L0, extra_after=0, extra_jump=False):
    x = np.arange(n, dtype=float)
    c = 100.0 * np.exp(0.002 * x)
    c[-1] = c[-2] * 1.06  # signal bar: +6% >> 1.25*ATR (~0.8%)
    tail = []
    for j in range(extra_after):
        tail.append(c[-1] * (1.08 ** (j + 1) if extra_jump else 1.0 + 0.001 * (j + 1)))
    c = np.r_[c, tail]
    m = len(c)
    o = np.r_[c[0], c[:-1]]
    idx = pd.to_datetime(last_open - (n - 1 - np.arange(m)) * DAY, unit="s", utc=True)
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.003, "low": np.minimum(o, c) * 0.997,
                         "close": c, "volume": 1e3}, index=idx)


def _flat(n=150, last_open=L0):
    x = np.arange(n, dtype=float)
    c = 100 + np.sin(x / 5)
    idx = pd.to_datetime(last_open - (n - 1 - np.arange(n)) * DAY, unit="s", utc=True)
    return pd.DataFrame({"open": c, "high": c * 1.003, "low": c * 0.997, "close": c, "volume": 1e3}, index=idx)


def _world(extra_after=0, extra_jump=False):
    bars = {"BTC/USD": _breakout_bars(extra_after=extra_after, extra_jump=extra_jump),
            "ETH/USD": _flat(150 + extra_after, L0 + extra_after * DAY),
            "SOL/USD": _flat(150 + extra_after, L0 + extra_after * DAY)}
    return bars, {s: _tick(float(b["close"].iloc[-1])) for s, b in bars.items()}


def _vb(gw, now, **kw):
    s = _settings(volbrk_sleeve_enabled=True, max_exposure_fraction=0.75, **kw)
    sl = VolBrkSleeve(s, gateway=gw, now_fn=lambda: now)
    gw.settings = sl.settings
    return sl


def _st():
    return json.loads(Path("logs/volbrk_sleeve.json").read_text())


def test_entry_next_cycle_risk_sized_with_26_10_fills_and_tagged():
    bars, ticks = _world()
    now = L0 + DAY + 600
    sl = _vb(FakeGateway(bars, ticks), now)
    assert sl.fill_model.taker_fee_bps == 26.0 and sl.fill_model.slippage_bps == 10.0
    res = sl.run_cycle()
    assert res.symbols["BTC/USD"].startswith("BOUGHT paper"), res.symbols
    assert "No volbrk entry" in res.symbols["ETH/USD"]
    ent = [a for a in res.actions if a["event"] == "entry"]
    assert len(ent) == 1 and ent[0]["sleeve"] == SLEEVE
    pos = _st()["positions"]["BTC/USD"]
    assert pos["entry_bar_open"] == L0 + DAY and pos["k"] == 1.25
    price = ticks["BTC/USD"]["ask"]
    want = 500 * 0.0025 / (2.0 * pos["atr_prev"] / price)
    assert ent[0]["notional"] == pytest.approx(min(want, 125.0), rel=0.03)
    assert ent[0]["fee"] == pytest.approx(ent[0]["notional"] * 0.0026, rel=0.02)
    reg = SleeveRegistry(); reg.load()
    assert reg.owner_of("BTC/USD") == SLEEVE
    # the fast exit watcher must not apply the primary 3% stop / 25% TP to this lot
    sleeve, stop, tp, _ = spot_exit_levels(sl.settings, "BTC/USD", pos["entry"])
    assert (sleeve, stop, tp) == ("volbrk_1d", None, None)
    assert sleeve_strategy_key(sl.settings, "volbrk_1d") == "volbrk_1d"


def test_stale_signal_not_chased():
    bars, ticks = _world()
    res = _vb(FakeGateway(bars, ticks), L0 + 2 * DAY + 600).run_cycle()
    assert "not chased" in res.symbols["BTC/USD"] and not res.actions


def test_time_stop_exits_after_exactly_10_daily_bars():
    bars, ticks = _world()
    _vb(FakeGateway(bars, ticks), L0 + DAY + 600).run_cycle()
    # 9 bars after the signal bar completed -> 9 held -> still holding
    bars9, t9 = _world(extra_after=9)
    res = _vb(FakeGateway(bars9, t9), L0 + 10 * DAY + 600).run_cycle()
    assert "holding volbrk 9/10" in res.symbols["BTC/USD"] and not res.actions
    # 10th bar completes -> time stop at the next open
    bars10, t10 = _world(extra_after=10)
    res = _vb(FakeGateway(bars10, t10), L0 + 11 * DAY + 600).run_cycle()
    ex = [a for a in res.actions if a["event"] == "exit"]
    assert len(ex) == 1 and ex[0]["reason"] == "time_stop_10d" and ex[0]["sleeve"] == SLEEVE
    assert "BTC/USD" not in _st()["positions"]
    assert "BTC/USD" not in json.loads(Path("logs/paper_bot_positions.json").read_text())


def test_signal_on_exit_bar_is_ignored_like_research():
    bars, ticks = _world()
    _vb(FakeGateway(bars, ticks), L0 + DAY + 600).run_cycle()
    # bar i+10 is itself a huge breakout; research ignores it (left counter)
    bars10, t10 = _world(extra_after=10, extra_jump=True)
    assert volbrk_signal(bars10["BTC/USD"])["fire"]
    res = _vb(FakeGateway(bars10, t10), L0 + 11 * DAY + 600).run_cycle()
    assert [a["event"] for a in res.actions] == ["exit"]
    assert "BTC/USD" not in _st()["positions"]


def test_disabled_flag_and_locks():
    assert not sleeve_active(_settings(volbrk_sleeve_enabled=False))[0]
    assert sleeve_active(_settings(volbrk_sleeve_enabled=True))[0]
    with pytest.raises(Exception):
        _settings(volbrk_k=2.0)  # configurable only within 1.0-1.5

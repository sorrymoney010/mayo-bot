"""Daily risk-on filter (D1) + 4h trend-hold paper sleeve."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from dublin_bot.backtest_core import Costs, Spec, add_indicators, simulate, trendhold_signals
from dublin_bot.daily_filter import DAY, DailyFilter, attach_d1, d1_mask, riskon_table
from dublin_bot.models import Action
from dublin_bot.sleeve_registry import SleeveRegistry
from dublin_bot.trendhold_sleeve import SLEEVE, TrendHoldSleeve

sys.path.insert(0, str(Path(__file__).resolve().parent))  # reuse sibling test helpers
from test_meanrev_sleeve import NOW, TF, FakeGateway, _settings, _tick

D0 = 1_700_006_400  # a UTC midnight


def _daily(n=120, start=D0, rising=True):
    x = np.arange(n, dtype=float)
    close = 100.0 + (x if rising else -0.5 * x)
    return pd.DataFrame({"time": start + x.astype(int) * DAY, "close": close})


def _filter(rising=True, start=D0, n=120, now=None):
    now = now if now is not None else start + (n + 5) * DAY
    return DailyFilter(fetch=lambda sym: _daily(n, start, rising), now_fn=lambda: now)


# ── D1 table ───────────────────────────────────────────────────
def test_riskon_table_closed_days_and_warmup():
    tab = riskon_table(_daily(120), now=D0 + 100 * DAY + 3600)  # day 100 still open
    assert len(tab) == 100
    assert tab["close_time"].iloc[-1] == D0 + 100 * DAY
    assert tab["riskon"].iloc[:54].isna().all()  # SMA50 + 5d rise need 55 closed days
    assert (tab["riskon"].iloc[54:] == 1.0).all()
    down = riskon_table(_daily(120, rising=False), now=D0 + 200 * DAY)
    assert (down["riskon"].iloc[54:] == 0.0).all()


def test_attach_d1_has_no_lookahead():
    tab = pd.DataFrame({"close_time": [D0 + DAY, D0 + 2 * DAY], "close": [1.0, 2.0],
                        "sma50": [1.0, 1.0], "sma50_prev": [0.5, 0.5], "riskon": [1.0, 0.0]})
    opens = np.array([D0 + DAY - 4 * 3600, D0 + 2 * DAY - 4 * 3600, D0 + 2 * DAY + 20 * 3600])
    d = attach_d1(pd.DataFrame({"time": opens, "close": [1, 1, 1]}), tab, 240)
    # a bar closing exactly at a daily close sees that day; a bar closing later sees
    # the latest CLOSED day only - never a later (still-forming) day.
    assert list(d["d1_riskon"]) == [1.0, 0.0, 0.0]
    early = attach_d1(pd.DataFrame({"time": [D0 - 3 * 3600.0], "close": [1]}), tab, 60)
    assert np.isnan(early["d1_riskon"].iloc[0])


def test_d1_mask_fails_closed():
    assert not d1_mask(pd.DataFrame({"close": [1, 2]})).any()
    assert list(d1_mask(pd.DataFrame({"d1_riskon": [np.nan, 1.0, 0.0]}))) == [False, True, False]
    assert DailyFilter(fetch=lambda s: (_ for _ in ()).throw(OSError("down"))).table("BTC/USD") is None


# ── strategies: D1 gates entries only ─────────────────────────
def _regime_case():
    from dublin_bot.config import Settings
    from dublin_bot.strategy import build_strategy
    from test_backtest_walkforward import _frame, _trend_then_chop

    bars = _frame(_trend_then_chop()).drop(columns=["time"])
    start = pd.Timestamp("2026-01-01", tz="UTC")
    bars.index = pd.date_range(start, periods=len(bars), freq="h", tz="UTC")
    s = Settings(_env_file=None, strategy="regime_trend", stop_loss_pct=0.03, take_profit_pct=0.25,
                 regime_lookback=20, regime_atr_mult=3.0)
    assert s.regime_daily_filter is True  # on by default
    return build_strategy(s), bars, int(start.timestamp()) - 100 * DAY


def _first_regime_buy(strat, bars):
    for end in range(250, 420):
        if strat.evaluate(bars.iloc[: end + 1], in_position=False).action is Action.BUY:
            return end
    return None


def test_regime_d1_riskon_keeps_entries_riskoff_blocks_them():
    strat, bars, dstart = _regime_case()
    strat.daily_filter = _filter(True, start=dstart, n=140)
    end = _first_regime_buy(strat, bars)
    assert end is not None
    strat.daily_filter = _filter(False, start=dstart, n=140)
    out = strat.evaluate(bars.iloc[: end + 1], in_position=False)
    assert out.action is not Action.BUY and "D1 risk-off" in out.reason


def test_regime_d1_unknown_blocks_entries_offline():
    strat, bars, _ = _regime_case()  # conftest: live daily fetch fails -> fail closed
    assert _first_regime_buy(strat, bars) is None


def test_regime_d1_never_forces_or_blocks_exit():
    strat, bars, dstart = _regime_case()
    strat.daily_filter = _filter(False, start=dstart, n=140)
    assert strat.evaluate(bars, in_position=True).action is Action.SELL
    strat.daily_filter = _filter(True, start=dstart, n=140)
    held = strat.evaluate(bars.iloc[:330], in_position=True)
    assert held.action is not Action.SELL or "D1" not in held.reason


def test_meanrev_sleeve_d1_blocks_new_limit():
    from dublin_bot.meanrev_sleeve import MeanRevSleeve
    from test_meanrev_sleeve import _bars

    bars = {s: _bars("up") for s in ("BTC/USD", "ETH/USD", "SOL/USD")}
    bars["BTC/USD"] = _bars("dip")
    ticks = {s: _tick(float(b["close"].iloc[-1])) for s, b in bars.items()}
    gw = FakeGateway(bars, ticks)
    sl = MeanRevSleeve(_settings(meanrev_daily_filter=True), gateway=gw, now_fn=lambda: NOW)
    gw.settings = sl.settings
    sl.strategy.daily_filter = _filter(False, start=int(NOW) - 130 * DAY, n=128, now=NOW)
    res = sl.run_cycle()
    assert "D1 risk-off" in res.symbols["BTC/USD"]
    assert not json.loads(Path("logs/meanrev_sleeve.json").read_text())["pending"]


# ── trend-hold backtest family ────────────────────────────────
def _trend_frame(n=400, drop_last=0, start=0):
    x = np.arange(n, dtype=float)
    c = 100.0 * np.exp(0.002 * x)
    if drop_last:
        c[-drop_last:] = c[-drop_last - 1] * np.cumprod(np.full(drop_last, 0.97))
    t = start + x.astype(int) * TF
    return pd.DataFrame({"time": t, "open": c, "high": c * 1.002, "low": c * 0.998, "close": c,
                         "volume": np.full(n, 1000.0)})


def test_trendhold_signals_and_simulate_exit_below_ema100():
    d = add_indicators(_trend_frame(400, drop_last=15))
    d["d1_riskon"] = 1.0
    p = {"ema_fast": 20, "ema_slow": 100, "d1": True, "warm": 120}
    sig = trendhold_signals(d, p)
    assert sig["entry"][200] and not sig["entry"][:99].any()  # warm-up >= slow EMA
    trades = simulate(d, Spec("trendhold", p), Costs())
    assert len(trades) == 1 and trades[0].reason == "below_ema100"
    assert trades[0].entry_i == 121  # next bar's open after the first eligible close
    d["d1_riskon"] = 0.0
    assert simulate(d, Spec("trendhold", p), Costs()) == []


# ── trend-hold paper sleeve ───────────────────────────────────
def _bars_4h(kind="trend", n=300):
    x = np.arange(n, dtype=float)
    if kind == "trend":
        c = 100.0 * np.exp(0.003 * x)
    elif kind == "flat":
        c = 100.0 + np.sin(x / 5.0)
    else:  # trend then a sharp break below EMA100
        c = 100.0 * np.exp(0.003 * x)
        c[-6:] = c[-7] * np.cumprod(np.full(6, 0.93))
    last_open = NOW - 600 - TF
    idx = pd.to_datetime([last_open - (n - 1 - i) * TF for i in range(n)], unit="s", utc=True)
    return pd.DataFrame({"open": c, "high": c * 1.003, "low": c * 0.997, "close": c,
                         "volume": np.full(n, 1000.0)}, index=idx)


def _world(btc="trend"):
    bars = {"BTC/USD": _bars_4h(btc), "ETH/USD": _bars_4h("flat"), "SOL/USD": _bars_4h("flat")}
    ticks = {s: _tick(float(b["close"].iloc[-1])) for s, b in bars.items()}
    return bars, ticks


def _th(gw, now=NOW, rising=True, **kw):
    s = _settings(trendhold_sleeve_enabled=True, max_exposure_fraction=0.75, **kw)
    sl = TrendHoldSleeve(s, gateway=gw, now_fn=lambda: now)
    gw.settings = sl.settings
    sl.strategy.daily_filter = _filter(rising, start=int(NOW) - 130 * DAY, n=128, now=now)
    return sl


def test_trendhold_enters_25pct_and_registers_ownership():
    bars, ticks = _world()
    res = _th(FakeGateway(bars, ticks)).run_cycle()
    assert res.symbols["BTC/USD"].startswith("BOUGHT paper")
    entry = [a for a in res.actions if a["event"] == "entry"]
    assert len(entry) == 1 and entry[0]["notional"] == pytest.approx(125.0, rel=0.02)
    reg = SleeveRegistry()
    reg.load()
    assert reg.owner_of("BTC/USD") == SLEEVE
    assert json.loads(Path("logs/paper_bot_positions.json").read_text())["BTC/USD"] > 0


def test_trendhold_d1_riskoff_blocks_entry():
    bars, ticks = _world()
    res = _th(FakeGateway(bars, ticks), rising=False).run_cycle()
    assert "D1 risk-off" in res.symbols["BTC/USD"]
    assert not res.actions


def test_trendhold_exits_on_first_close_below_ema100_without_stop():
    bars, ticks = _world()
    _th(FakeGateway(bars, ticks)).run_cycle()
    later = NOW + 2 * TF
    bars2 = {s: b.set_axis(b.index + pd.Timedelta(seconds=2 * TF)) for s, b in _world("break")[0].items()}
    ticks2 = {s: _tick(float(b["close"].iloc[-1])) for s, b in bars2.items()}
    res = _th(FakeGateway(bars2, ticks2), now=later).run_cycle()
    ex = [a for a in res.actions if a["event"] == "exit"]
    assert len(ex) == 1 and ex[0]["reason"] == "below_ema100"
    assert "BTC/USD" not in json.loads(Path("logs/paper_bot_positions.json").read_text())
    reg = SleeveRegistry()
    reg.load()
    assert reg.owner_of("BTC/USD") != SLEEVE


def test_trendhold_skips_coin_owned_by_another_sleeve():
    bars, ticks = _world()
    reg = SleeveRegistry()
    reg.load()
    reg.set_sleeve("meanrev_4h", owned={"BTC/USD"}, pending={})
    reg.save()
    res = _th(FakeGateway(bars, ticks)).run_cycle()
    assert "another sleeve" in res.symbols["BTC/USD"]


def test_trendhold_max_positions_counts_every_sleeve():
    bars, ticks = _world()
    Path("logs").mkdir(exist_ok=True)
    Path("logs/paper_bot_positions.json").write_text(json.dumps({"ETH/USD": 0.01, "SOL/USD": 0.01}))
    res = _th(FakeGateway(bars, ticks), max_concurrent_positions=2).run_cycle()
    assert "max concurrent positions" in res.symbols["BTC/USD"]


def test_trendhold_respects_total_exposure_cap():
    bars, ticks = _world()
    ticks["ETH/USD"] = _tick(30_000.0)  # other sleeves already hold $300 of ETH
    Path("logs").mkdir(exist_ok=True)
    Path("logs/paper_bot_positions.json").write_text(json.dumps({"ETH/USD": 0.01}))
    res = _th(FakeGateway(bars, ticks)).run_cycle()
    entry = [a for a in res.actions if a["event"] == "entry"]
    # cap = 75% x $500 = $375 -> only $75 of room left, never the full $125
    # (sized at the ask; the fill's 10 bps slippage may add a few cents)
    assert not entry or entry[0]["notional"] <= 75.0 * 1.002


def test_three_sleeves_fit_under_shared_caps():
    from dublin_bot.config import Settings

    s = Settings(_env_file=None)
    assert s.trendhold_position_fraction * s.max_concurrent_positions <= 0.75 + 1e-9
    assert s.trendhold_sleeve_enabled and s.regime_daily_filter and s.meanrev_daily_filter
    wrapper = (Path(__file__).resolve().parents[1] / "scripts" / "run_paper_mac.sh").read_text()
    for line in ("MAX_CONCURRENT_POSITIONS=3", "MAX_EXPOSURE_FRACTION=0.75", "TRENDHOLD_SLEEVE_ENABLED=true",
                 "REGIME_DAILY_FILTER=true", "MEANREV_DAILY_FILTER=true"):
        assert line in wrapper


def test_trendhold_universe_is_btc_eth_sol_regardless_of_allowlist():
    bars, ticks = _world()
    sl = _th(FakeGateway(bars, ticks), universe_allowlist=["PUMP/USD", "BTC/USD"],
             trendhold_symbols=["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD"])
    assert sl.universe() == ["BTC/USD", "ETH/USD", "SOL/USD"]

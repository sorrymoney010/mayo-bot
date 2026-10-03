"""Paper-only professional features: trailing TP, fast exits, futures/margin locks, research."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from dublin_bot.book_risk import futures_equity_addon
from dublin_bot.config import Settings
from dublin_bot.exit_watcher import CandleBook, ExitWatcher
from dublin_bot.futures_costs import (
    MAX_PAPER_LEVERAGE,
    clamp_leverage,
    funding_cashflow,
    liquidation_price,
    relative_funding_rate,
)
from dublin_bot.futures_public import FuturesPublic
from dublin_bot.futures_sleeve import FuturesSleeve, sleeve_active
from dublin_bot.margin_paper import margin_borrow_cost
from dublin_bot.paper import PaperPortfolio
from dublin_bot.research import improves, library, register_shadow, search, shadow_cycle
from dublin_bot.trailing import TrailBook, fresh_state, on_bar, on_price
from dublin_bot.venue_locks import (
    VenueLockError,
    live_futures_submission_allowed,
    live_margin_submission_allowed,
    submit_futures_order,
    submit_margin_order,
)


def _settings(**over) -> Settings:
    return Settings(_env_file=None, timeframe_minutes=60, **over)


def test_existing_locks_unchanged_and_new_locks_default_off():
    s = _settings()
    assert s.paper_trading and s.dry_run and not s.allow_live_trading
    assert s.safety_locked
    assert s.allow_futures_live_orders is False
    assert s.allow_margin_live_orders is False
    assert s.futures_sleeve_enabled is False
    assert s.trailing_tp_regime is False
    assert s.trailing_tp_meanrev is False
    assert s.trailing_tp_trendhold is False
    assert s.futures_leverage <= MAX_PAPER_LEVERAGE


def test_settings_refuse_futures_leverage_above_two():
    with pytest.raises(ValueError):
        _settings(futures_leverage=3)


def test_env_example_keeps_the_three_locks_and_adds_venue_locks():
    text = Path("README.md").resolve().parents[0]
    # conftest chdirs to tmp; the repo root is the parent of tests/
    root = Path(__file__).resolve().parents[1]
    env = (root / ".env.example").read_text()
    assert "PAPER_TRADING=true" in env
    assert "DRY_RUN=true" in env
    assert "ALLOW_LIVE_TRADING=false" in env
    assert "ALLOW_FUTURES_LIVE_ORDERS=false" in env
    assert "ALLOW_MARGIN_LIVE_ORDERS=false" in env
    assert text  # silence unused if the path helper stays


def test_venue_locks_refuse_unless_every_lock_is_open():
    calls = []

    def transport(path, payload):
        calls.append((path, payload))
        return {"ok": True}

    base = _settings()
    for fn, settings in (
        (submit_futures_order, base),
        (submit_margin_order, base),
        (submit_futures_order, _settings(allow_futures_live_orders=True)),
        (submit_margin_order, _settings(allow_margin_live_orders=True)),
        # Venue flag on and the three locks opened, but the OTHER venue flag stays off.
        (submit_margin_order, _settings(
            allow_futures_live_orders=True, paper_trading=False, dry_run=False,
            allow_live_trading=True, live_risk_acknowledgement="I_ACCEPT_LIVE_TRADING_RISK")),
        (submit_futures_order, _settings(
            allow_margin_live_orders=True, paper_trading=False, dry_run=False,
            allow_live_trading=True, live_risk_acknowledgement="I_ACCEPT_LIVE_TRADING_RISK")),
    ):
        with pytest.raises(VenueLockError):
            fn(settings, {"leverage": "2"}, transport)
    assert calls == []

    # The venue flag alone, with the existing locks still shut, is not enough.
    assert live_futures_submission_allowed(_settings(allow_futures_live_orders=True))[0] is False
    assert live_margin_submission_allowed(_settings(allow_margin_live_orders=True))[0] is False

    opened = _settings(
        allow_futures_live_orders=True, allow_margin_live_orders=True,
        paper_trading=False, dry_run=False, allow_live_trading=True,
        live_risk_acknowledgement="I_ACCEPT_LIVE_TRADING_RISK",
    )
    assert submit_futures_order(opened, {"leverage": "2"}, transport)["ok"] is True
    assert submit_margin_order(opened, {"leverage": "2"}, transport)["ok"] is True
    assert len(calls) == 2
    assert calls[0][0].endswith("sendorder")
    assert "AddOrder" in calls[1][0]


def test_futures_sleeve_module_has_no_order_route():
    src = Path(__file__).resolve().parents[1] / "src" / "dublin_bot" / "futures_sleeve.py"
    text = src.read_text()
    assert "sendorder" not in text
    assert "AddOrder" not in text
    pub = (src.parent / "futures_public.py").read_text()
    assert "sendorder" not in pub


def test_trailing_arms_ratchets_and_survives_restart(tmp_path):
    st = fresh_state(sleeve="primary", symbol="BTC/USD", side="long", entry=100, atr=2,
                     activate_atr=1.5, trail_atr=1.0, opened_at="t0")
    st, reason = on_price(st, 102)
    assert reason is None and st.armed is False
    st, reason = on_price(st, 104)  # +4 >= 1.5*2, arm at 104-2
    assert reason is None and st.armed and st.stop == pytest.approx(102)
    st, reason = on_price(st, 106)  # ratchet
    assert reason is None and st.stop == pytest.approx(104)
    st, reason = on_price(st, 103.5)
    assert reason == "trail"

    armed = fresh_state(sleeve="primary", symbol="ETH/USD", side="long", entry=10, atr=1,
                        activate_atr=1.0, trail_atr=1.0, opened_at="t1", last_price=12)
    book = TrailBook(tmp_path / "trail.json")
    book.put(armed)
    book.save()
    again = TrailBook(tmp_path / "trail.json")
    restored = again.states[armed.key()]
    assert restored.armed and restored.peak == pytest.approx(12)
    again.reconcile(set())
    assert again.states == {}


def test_trailing_short_and_bar_gap():
    st = fresh_state(sleeve="futures_short", symbol="BTC/USD", side="short", entry=100, atr=2,
                     activate_atr=1.5, trail_atr=1.0)
    st, reason = on_price(st, 96)  # +4 in favor
    assert st.armed and reason is None
    assert st.stop == pytest.approx(98)
    st, reason = on_price(st, 99)
    assert reason == "trail"
    gap = fresh_state(sleeve="x", symbol="S", side="long", entry=100, atr=1,
                      activate_atr=1, trail_atr=1, last_price=102)
    gap, why, px = on_bar(gap, open_=90, high=91, low=89)
    assert why == "trail_gap" and px == pytest.approx(90)


def test_backtest_overlay_can_exit_on_a_pullback():
    from dublin_bot.backtest_core import Costs, Spec, add_indicators, simulate
    n = 80
    close = np.concatenate([np.linspace(100, 130, 50), np.linspace(129, 110, 30)])
    df = pd.DataFrame({
        "time": np.arange(n) * 3600,
        "open": close,
        "high": close + 0.4,
        "low": close - 0.4,
        "close": close,
        "volume": np.full(n, 10.0),
    })
    d = add_indicators(df)
    d["d1_riskon"] = 1.0
    spec = Spec("trendhold", {"ema_fast": 3, "ema_slow": 8, "d1": True, "warm": 15,
                              "trail_activate_atr": 0.5, "trail_atr": 0.5})
    trades = simulate(d, spec, Costs(fee_bps=40, slippage_bps=10, maker_bps=25))
    assert trades
    assert any(t.reason.startswith("trail") for t in trades)


def test_fast_exit_fires_on_a_tick_and_does_not_double_close(tmp_path, monkeypatch):
    monkeypatch.setenv("MAYO_LEDGER_OWNER", "1")
    s = _settings(stop_loss_pct=0.05, take_profit_pct=0.25)
    book = PaperPortfolio(tmp_path / "paper_portfolio.json")
    book.load(equity=500, cash=500)
    book.record_buy("BTC/USD", 0.01, 100.0, 0.1, "2026-01-01T00:00:00+00:00")
    watcher = ExitWatcher(s, portfolio=book, require_owner=True)
    candles = CandleBook()
    candles.add("BTC/USD", 1_700_000_000, 100)
    candles.add("BTC/USD", 1_700_000_030, 101)
    assert candles.current["BTC/USD"].trades == 2
    candles.add("BTC/USD", 1_700_000_060, 90)
    assert len(candles.history["BTC/USD"]) == 1
    closed = watcher.run_once([("BTC/USD", 1_700_000_100, 90.0)])
    assert len(closed) == 1 and closed[0]["reason"] == "stop"
    again = watcher.run_once([("BTC/USD", 1_700_000_110, 80.0)])
    assert again == []
    snap = book.load(equity=500, cash=500)
    assert "BTC/USD" not in snap.positions


def test_fast_exit_refuses_without_ledger_owner(tmp_path, monkeypatch):
    monkeypatch.delenv("MAYO_LEDGER_OWNER", raising=False)
    watcher = ExitWatcher(_settings(), portfolio=PaperPortfolio(tmp_path / "p.json"), require_owner=True)
    assert watcher.allowed()[0] is False
    assert watcher.run_once([("BTC/USD", 1.0, 1.0)])[0]["refused"]


def test_two_portfolios_cannot_sell_the_same_lot(tmp_path):
    path = tmp_path / "paper_portfolio.json"
    a, b = PaperPortfolio(path), PaperPortfolio(path)
    a.load(equity=100, cash=100)
    a.record_buy("BTC/USD", 1, 10, 0, "t")
    first = b.try_record_sell("BTC/USD", 1, 12, 0, "t2")
    second = a.try_record_sell("BTC/USD", 1, 12, 0, "t3")
    assert first is not None and second is None


def test_futures_public_parser_and_costs():
    payload = {"tickers": [{
        "symbol": "PF_XBTUSD", "last": "100", "markPrice": "101",
        "fundingRate": "0.0001", "openInterest": "50", "bid": "100", "ask": "101",
    }]}

    def opener(url, timeout):
        return json.dumps(payload).encode()

    snap = FuturesPublic(opener=opener).snapshot("PF_XBTUSD")
    assert snap["mark"] == pytest.approx(101)
    assert snap["funding_rate"] == pytest.approx(0.0001)
    # Ticker fundingRate is quote per base unit. Cash uses it divided by the mark.
    assert snap["funding_rate_relative"] == pytest.approx(0.0001 / 101)
    rel = relative_funding_rate(-0.09291925424702825, 84711.43537236296)
    assert rel == pytest.approx(-0.09291925424702825 / 84711.43537236296)
    assert funding_cashflow(side="short", notional=10_000, hourly_rate=rel, hours=1) == pytest.approx(rel * 10_000)
    assert snap["open_interest"] == pytest.approx(50)
    assert liquidation_price(100, side="short", leverage=2, maintenance=0.01) == pytest.approx(149)
    assert liquidation_price(100, side="long", leverage=2, maintenance=0.01) == pytest.approx(51)
    assert clamp_leverage(9) == 2
    # Positive funding: short receives.
    assert funding_cashflow(side="short", notional=10_000, hourly_rate=0.0001, hours=1) == pytest.approx(1.0)
    assert funding_cashflow(side="long", notional=10_000, hourly_rate=0.0001, hours=1) == pytest.approx(-1.0)


def test_futures_equity_does_not_double_count_funding_already_in_cash(tmp_path):
    path = tmp_path / "paper_futures.json"
    path.write_text(json.dumps({
        "positions": {"BTC/USD": {
            "qty": -1, "margin": 250.0, "unrealized": -10.0, "funding_usd": 4.0,
            "notional": 500.0,
        }},
    }), encoding="utf-8")
    # Cash already received the $4 funding payment. Addon is margin + unrealized only.
    assert futures_equity_addon(path) == pytest.approx(240.0)


def test_futures_sleeve_opens_a_capped_short_and_stays_off_by_default(tmp_path):
    assert sleeve_active(_settings())[0] is False
    s = _settings(
        futures_sleeve_enabled=True, futures_leverage=2, futures_ema_fast=2, futures_ema_slow=10,
        futures_symbols=["BTC/USD"],
        futures_ledger_path=tmp_path / "fut.json", strategy_equity_usd=500,
        max_exposure_fraction=0.75, max_concurrent_positions=3,
    )
    n = 40
    close = pd.Series(np.linspace(100, 60, n))
    bars = pd.DataFrame({"open": close, "high": close + 0.5, "low": close - 0.5,
                         "close": close, "volume": 5.0})
    bars.index = pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC")
    quotes = {"mark": 60.0, "last": 60.0, "funding_rate": 0.0001, "open_interest": 12.0}

    class Pub:
        def snapshot(self, symbol):
            return {"symbol": symbol, **quotes}

    book = PaperPortfolio(tmp_path / "paper_portfolio.json")
    book.load(equity=500, cash=500)
    sleeve = FuturesSleeve(
        s, public=Pub(), portfolio=book, ledger_path=s.futures_ledger_path,
        bars_fn=lambda _sym: bars, daily_fn=lambda _sym: {"riskon": False, "reason": "ok"},
    )
    res = sleeve.run_cycle()
    assert res.active
    assert any(a["event"] == "entry" for a in res.actions)
    pos = json.loads(Path(s.futures_ledger_path).read_text())["positions"]["BTC/USD"]
    assert pos["leverage"] <= 2
    assert pos["liq"] > pos["entry"]
    assert pos["notional"] <= 500 * 0.75 + 1e-6
    # Bullish daily filter blocks a new name.
    s2 = s.model_copy(update={"futures_symbols": ["ETH/USD"]})
    blocked = FuturesSleeve(
        s2, public=Pub(), portfolio=book, ledger_path=s.futures_ledger_path,
        bars_fn=lambda _sym: bars, daily_fn=lambda _sym: {"riskon": True, "reason": "ok"},
    ).run_cycle()
    assert "ETH/USD" not in json.loads(Path(s.futures_ledger_path).read_text())["positions"]
    assert "no short" in blocked.symbols["ETH/USD"]


def test_futures_sleeve_refuses_when_a_safety_lock_is_open():
    s = _settings(futures_sleeve_enabled=True, paper_trading=False, dry_run=False,
                  allow_live_trading=True, live_risk_acknowledgement="I_ACCEPT_LIVE_TRADING_RISK")
    assert sleeve_active(s)[0] is False


def test_margin_cost_uses_published_band_and_does_not_replace_the_short():
    btc = margin_borrow_cost("BTC/USD", 10_000, hours_open=8)
    assert btc["opening_fee_usd"] == pytest.approx(2.0)  # 0.02%
    assert btc["rollover_blocks"] == 2
    assert btc["rollover_fee_usd"] == pytest.approx(4.0)
    eth = margin_borrow_cost("ETH/USD", 10_000, hours_open=4)
    assert eth["opening_fee_rate"] == pytest.approx(0.0004)
    assert "perpetuals cover shorting" in btc["note"]


def test_research_library_skips_momentum_and_sub_hour_and_can_register_shadow(tmp_path):
    libs = library()
    assert "momentum" not in libs and "breakout" not in libs
    for specs in libs.values():
        assert specs[0].name not in {"momentum", "breakout"}
    n = 80
    df = pd.DataFrame({
        "time": np.arange(n) * 3600, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1,
    })
    from dublin_bot.backtest_core import Costs, add_indicators
    rep = search({15: [add_indicators(df)], 60: [add_indicators(df)]}, Costs())
    assert any(r.get("refused") for r in rep["results"])
    assert rep["winners"] == []
    assert improves(1, 2, 1, 2, 5, 5, 10) is True
    assert improves(1, 2, 1, 2, 5, 5, 3) is False  # too few trades
    winner = {"family": "regime", "timeframe_minutes": 60, "oos": {"trades": 40}, "passes": True}
    rec = register_shadow(winner, path=tmp_path / "shadow.json", report="reports/x.json")
    assert rec["fills"] is False and rec["mode"] == "shadow"
    # Re-registering the same day is idempotent.
    register_shadow(winner, path=tmp_path / "shadow.json")
    assert len(json.loads((tmp_path / "shadow.json").read_text())) == 1
    before = _settings()
    out = shadow_cycle(before, fetch_bars=lambda *_a: df, shadow_path=tmp_path / "shadow.json")
    assert isinstance(out, list)
    after = _settings()
    assert after.paper_trading == before.paper_trading
    assert after.allow_live_trading is False and after.dry_run is True


def test_exit_watcher_script_refuses_without_owner(tmp_path, monkeypatch):
    import os
    import shutil
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "scripts").mkdir()
    shutil.copy(root / "scripts" / "exit_watcher.py", tmp_path / "scripts")
    import site
    env = os.environ.copy()
    env.pop("MAYO_LEDGER_OWNER", None)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(root / "src"), site.getusersitepackages(), env.get("PYTHONPATH", "")]
    )
    env["PAPER_LOCK_WAIT_SECONDS"] = "0"
    env["HOME"] = str(tmp_path)
    proc = subprocess.run([sys.executable, str(tmp_path / "scripts" / "exit_watcher.py")],
                          env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 3
    assert "ledger owner" in proc.stdout

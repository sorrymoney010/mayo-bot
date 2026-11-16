"""4h mean-reversion PAPER sleeve: signals, post-only limits, expiry, exits,
cross-sleeve ownership/caps, learner tagging and the live-lock guard."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from dublin_bot.backtest_core import Costs, Spec, add_indicators, meanrev_signals, simulate
from dublin_bot.config import Settings
from dublin_bot.learner import LearningAgent
from dublin_bot.meanrev_sleeve import SLEEVE, MeanRevSleeve, sleeve_active
from dublin_bot.models import Action
from dublin_bot.paper import PaperPortfolio
from dublin_bot.sleeve_registry import PRIMARY, SleeveRegistry
from dublin_bot.strategies.meanrev4h_strategy import MeanReversion4hStrategy

TF = 240 * 60
NOW = 1_800_000_000.0  # fixed clock; last closed bar ends 10 min before NOW


def _settings(**kw) -> Settings:
    base = dict(
        _env_file=None, paper_trading=True, dry_run=True, allow_live_trading=False,
        strategy="regime_trend", timeframe_minutes=60, strategy_equity_usd=500.0,
        paper_use_ledger_equity=True, risk_per_trade=0.01, adaptive_risk=False,
        paper_taker_fee_bps=40.0, paper_maker_fee_bps=25.0, paper_slippage_bps=10.0,
        cooldown_minutes=0, max_orders_per_day=0, learner_priors_path="no-priors.json",
        universe_allowlist=["BTC/USD", "ETH/USD", "SOL/USD"], max_concurrent_positions=3,
        min_dollar_volume=0.0, meanrev_daily_filter=False,  # D1 covered in test_d1_trendhold.py
    )
    base.update(kw)
    return Settings(**base)


def _bars(kind: str, n: int = 300, end_close: float | None = None) -> pd.DataFrame:
    """kind='dip' → last bar washed out (RSI<38, below EMA50); 'up' → reverted."""
    x = np.arange(n, dtype=float)
    closes = 100.0 + 2.0 * np.sin(x / 9.0)
    if kind == "dip":
        closes[-8:] = closes[-9] * np.cumprod(np.full(8, 0.985))
    elif kind == "up":
        closes[-8:] = closes[-9] * np.cumprod(np.full(8, 1.02))
    if end_close is not None:
        closes = closes * (end_close / closes[-1])
    last_open = NOW - 600 - TF
    idx = pd.to_datetime([last_open - (n - 1 - i) * TF for i in range(n)], unit="s", utc=True)
    return pd.DataFrame({"open": closes, "high": closes * 1.004, "low": closes * 0.996,
                         "close": closes, "volume": np.full(n, 1000.0)}, index=idx)


class FakeGateway:
    def __init__(self, bars: dict[str, pd.DataFrame], tickers: dict[str, dict]):
        self.settings = None
        self.bars = bars
        self.tickers = tickers

    def get_bars(self):
        return self.bars[self.settings.symbol]

    def get_ticker_for(self, sym):
        return dict(self.tickers[sym])

    def market_quality(self):
        t = self.tickers[self.settings.symbol]
        return {"bid": t["bid"], "ask": t["ask"], "recent_dollar_volume": 1e9}

    def resolve_symbol(self, sym=None):
        return SimpleNamespace(order_min=Decimal("0.0001"), pair_decimals=2, cost_min=Decimal("0.5"))

    def size_buy(self, notional, price=None):
        return SimpleNamespace(volume=round(notional / price, 8), price=price)

    # Any order-submission call is a test failure: the sleeve is paper-only.
    def add_order(self, *a, **k):
        raise AssertionError("paper sleeve must never submit orders")

    buy_notional = close_position = add_order


def _tick(last, spread=0.02):
    return {"bid": last - spread / 2, "ask": last + spread / 2, "last": last,
            "volume_24h": 1e6, "vwap_24h": last}


def _sleeve(settings, gw, now=NOW):
    sl = MeanRevSleeve(settings, gateway=gw, now_fn=lambda: now)
    gw.settings = sl.settings
    return sl


def _state():
    return json.loads(Path("logs/meanrev_sleeve.json").read_text())


@pytest.fixture
def dip_world():
    bars = {s: _bars("up") for s in ("BTC/USD", "ETH/USD", "SOL/USD")}
    bars["BTC/USD"] = _bars("dip")
    close = float(bars["BTC/USD"]["close"].iloc[-1])
    ticks = {s: _tick(float(b["close"].iloc[-1])) for s, b in bars.items()}
    ticks["BTC/USD"] = _tick(close)
    return bars, ticks, close


# ── config ─────────────────────────────────────────────────────

def test_conservative_defaults_ported_from_dublin_and_locks_unchanged():
    s = Settings(_env_file=None)
    assert s.max_leverage == 1.0
    assert s.margin_exposure_fraction == 0.0
    assert s.risk_per_trade == 0.01
    assert s.max_position_fraction == 0.25
    assert s.paper_trading is True and s.dry_run is True and s.allow_live_trading is False
    assert s.safety_locked is True
    assert s.meanrev_sleeve_enabled is True
    assert s.meanrev_symbols == ["BTC/USD", "ETH/USD", "SOL/USD"]
    assert (s.meanrev_timeframe_minutes, s.meanrev_rsi_entry, s.meanrev_rsi_exit) == (240, 38.0, 55.0)
    assert (s.meanrev_stop_pct, s.meanrev_take_profit_pct, s.meanrev_limit_offset_pct) == (0.03, 0.25, 0.001)
    assert s.meanrev_limit_valid_bars == 1


def test_risk_pct_alias_still_maps_to_risk_per_trade(monkeypatch):
    monkeypatch.setenv("RISK_PCT", "0.005")
    assert Settings(_env_file=None).risk_per_trade == pytest.approx(0.005)


def test_meanrev_sleeve_env_toggle(monkeypatch):
    monkeypatch.setenv("MEANREV_SLEEVE_ENABLED", "false")
    s = Settings(_env_file=None)
    assert sleeve_active(s) == (False, "disabled (MEANREV_SLEEVE_ENABLED=false)")


# ── signal parity with the backtest ────────────────────────────

def test_live_signal_matches_backtest_rules():
    d = add_indicators(_bars("dip"))
    sig = meanrev_signals(d, {"rsi_os": 38.0, "rsi_exit": 55.0})
    legacy_entry = (d["rsi"] <= 38.0) & (d["close"] < d["ema50"])
    legacy_exit = (d["rsi"] >= 55.0) | (d["close"] >= d["ema50"])
    assert (sig["entry"] == legacy_entry.to_numpy()).all()
    assert (sig["exit"] == legacy_exit.to_numpy()).all()
    s = MeanReversion4hStrategy(_settings())
    buy = s.evaluate(_bars("dip"))
    assert buy.action is Action.BUY
    close = float(_bars("dip")["close"].iloc[-1])
    assert buy.price == pytest.approx(close * 0.999)
    assert buy.stop_price == pytest.approx(close * 0.999 * 0.97)
    assert s.evaluate(_bars("up"), in_position=True).action is Action.SELL
    assert s.evaluate(_bars("up")).action is Action.WAIT


def test_backtest_meanrev_still_runs_through_shared_signals():
    d = add_indicators(_bars("dip", n=400))
    d["time"] = np.arange(len(d))
    spec = Spec("meanrev", {"rsi_os": 38.0, "rsi_exit": 55.0, "stop": 0.03, "tp": 0.25,
                            "entry": "limit", "limit_offset": 0.001})
    trades = simulate(d, spec, Costs())
    assert isinstance(trades, list)


# ── lock guard ─────────────────────────────────────────────────

@pytest.mark.parametrize("update", [{"dry_run": False}, {"allow_live_trading": True},
                                    {"meanrev_sleeve_enabled": False}])
def test_sleeve_is_inert_unless_all_locks_engaged(update, dip_world):
    bars, ticks, _ = dip_world
    s = _settings().model_copy(update=update)
    gw = FakeGateway(bars, ticks)
    res = _sleeve(s, gw).run_cycle()
    assert res.active is False and not res.actions
    assert not Path("logs/meanrev_sleeve.json").exists()
    assert not Path("logs/paper_portfolio.json").exists()


# ── order lifecycle ────────────────────────────────────────────

def test_signal_places_post_only_limit_below_close_and_ask(dip_world):
    bars, ticks, close = dip_world
    gw = FakeGateway(bars, ticks)
    res = _sleeve(_settings(), gw).run_cycle()
    st = _state()
    order = st["pending"]["BTC/USD"]
    assert order["limit"] == pytest.approx(round(close * 0.999, 2))
    assert order["limit"] < ticks["BTC/USD"]["ask"]
    assert order["expires_at"] == pytest.approx(bars["BTC/USD"].index[-1].timestamp() + 2 * TF)
    # 1% risk / 3% stop = 33% of equity, capped by max_position_fraction 25% → ≤ $125
    assert 0 < order["notional"] <= 125.0 + 1e-6
    assert [a["event"] for a in res.actions] == ["limit_placed"]
    # Nothing is filled or charged yet; the order is published for the other sleeve.
    assert not Path("logs/paper_portfolio.json").exists() or \
        json.loads(Path("logs/paper_portfolio.json").read_text())["positions"] == []
    reg = SleeveRegistry()
    assert "BTC/USD" in reg.foreign_symbols(PRIMARY)
    assert reg.pending_notional(SLEEVE) == pytest.approx(order["notional"])
    # Same signal bar never places a second order.
    st["pending"].clear()
    Path("logs/meanrev_sleeve.json").write_text(json.dumps(st))
    res2 = _sleeve(_settings(), gw).run_cycle()
    assert "already handled" in res2.symbols["BTC/USD"]


def test_limit_fills_only_when_price_trades_through_at_maker_fee(dip_world):
    bars, ticks, close = dip_world
    gw = FakeGateway(bars, ticks)
    _sleeve(_settings(), gw).run_cycle()
    limit = _state()["pending"]["BTC/USD"]["limit"]
    # Price hovering above the limit: still resting.
    res = _sleeve(_settings(), gw, now=NOW + 60).run_cycle()
    assert "resting" in res.symbols["BTC/USD"]
    gw.tickers["BTC/USD"] = _tick(limit - 0.05)
    res = _sleeve(_settings(), gw, now=NOW + 120).run_cycle()
    assert [a["event"] for a in res.actions] == ["limit_filled"]
    st = _state()
    pos = st["positions"]["BTC/USD"]
    assert pos["entry"] == pytest.approx(limit)
    assert pos["stop"] == pytest.approx(limit * 0.97) and pos["tp"] == pytest.approx(limit * 1.25)
    assert pos["entry_fee"] == pytest.approx(pos["qty"] * limit * 0.0025)  # maker 25 bps
    book = json.loads(Path("logs/paper_portfolio.json").read_text())
    assert book["positions"][0]["symbol"] == "BTC/USD"
    assert json.loads(Path("logs/paper_bot_positions.json").read_text())["BTC/USD"] == pytest.approx(pos["qty"])
    assert SleeveRegistry().owner_of("BTC/USD") == SLEEVE


def test_unfilled_limit_expires_and_never_becomes_market_order(dip_world):
    bars, ticks, _ = dip_world
    gw = FakeGateway(bars, ticks)
    _sleeve(_settings(), gw).run_cycle()
    expires = _state()["pending"]["BTC/USD"]["expires_at"]
    gw.tickers["BTC/USD"] = _tick(ticks["BTC/USD"]["last"] * 1.01)  # ran away
    res = _sleeve(_settings(), gw, now=expires + 1).run_cycle()
    assert [a["event"] for a in res.actions] == ["limit_expired"]
    st = _state()
    assert st["pending"] == {} and st["positions"] == {}
    assert not Path("logs/paper_portfolio.json").exists() or \
        json.loads(Path("logs/paper_portfolio.json").read_text())["positions"] == []
    assert SleeveRegistry().foreign_symbols(PRIMARY) == set()


def _open_position(dip_world):
    bars, ticks, _ = dip_world
    gw = FakeGateway(bars, ticks)
    _sleeve(_settings(), gw).run_cycle()
    limit = _state()["pending"]["BTC/USD"]["limit"]
    gw.tickers["BTC/USD"] = _tick(limit)
    _sleeve(_settings(), gw, now=NOW + 60).run_cycle()
    return gw, limit


def test_stop_exit_books_loss_to_learner_under_sleeve_key(dip_world):
    gw, limit = _open_position(dip_world)
    gw.tickers["BTC/USD"] = _tick(limit * 0.965)
    res = _sleeve(_settings(), gw, now=NOW + 120).run_cycle()
    ex = [a for a in res.actions if a["event"] == "exit"][0]
    assert ex["reason"] == "stop" and ex["realized"] < 0
    assert _state()["positions"] == {}
    assert "BTC/USD" not in json.loads(Path("logs/paper_bot_positions.json").read_text())
    la = LearningAgent("logs/learner.json", strategy_key="meanrev_mk@240m")
    hist = la.coins["BTC/USD"].history
    assert hist[-1]["strategy"] == "meanrev_mk@240m" and hist[-1]["net_bps"] < 0
    # The regime sleeve's learner does not see this trade in its expectancy.
    assert LearningAgent("logs/learner.json", strategy_key="regime@60m")._live("BTC/USD")[1] == 0
    assert SleeveRegistry().owner_of("BTC/USD") == PRIMARY  # released


def test_signal_exit_waits_for_a_bar_closed_after_fill(dip_world):
    gw, limit = _open_position(dip_world)
    gw.bars["BTC/USD"] = _bars("up", end_close=limit * 1.02)
    gw.tickers["BTC/USD"] = _tick(limit * 1.02)
    # Last closed bar ended before the fill → hold.
    res = _sleeve(_settings(), gw, now=NOW + 120).run_cycle()
    assert "no bar closed since fill" in res.symbols["BTC/USD"]
    # Shift bars so a bar closed after the fill.
    shifted = gw.bars["BTC/USD"].copy()
    shifted.index = pd.to_datetime([t.timestamp() + TF for t in shifted.index], unit="s", utc=True)
    gw.bars["BTC/USD"] = shifted
    res = _sleeve(_settings(), gw, now=NOW + TF + 60).run_cycle()
    ex = [a for a in res.actions if a["event"] == "exit"][0]
    assert ex["reason"] == "reverted" and ex["realized"] > 0


def test_take_profit_exit_is_maker_at_tp_price(dip_world):
    gw, limit = _open_position(dip_world)
    gw.tickers["BTC/USD"] = _tick(limit * 1.26)
    res = _sleeve(_settings(), gw, now=NOW + 120).run_cycle()
    ex = [a for a in res.actions if a["event"] == "exit"][0]
    assert ex["reason"] == "tp" and ex["price"] == pytest.approx(limit * 1.25)


# ── cross-sleeve safety ────────────────────────────────────────

def test_symbol_held_by_primary_is_skipped(dip_world):
    bars, ticks, _ = dip_world
    Path("logs").mkdir(exist_ok=True)
    Path("logs/paper_bot_positions.json").write_text(json.dumps({"BTC/USD": 0.001}))
    res = _sleeve(_settings(), FakeGateway(bars, ticks)).run_cycle()
    assert "another sleeve" in res.symbols["BTC/USD"]
    assert _state()["pending"] == {}


def test_max_concurrent_positions_counts_every_sleeve(dip_world):
    bars, ticks, _ = dip_world
    Path("logs").mkdir(exist_ok=True)
    Path("logs/paper_bot_positions.json").write_text(json.dumps({"ETH/USD": 0.01, "SOL/USD": 0.1}))
    res = _sleeve(_settings(max_concurrent_positions=2), FakeGateway(bars, ticks)).run_cycle()
    assert "max concurrent positions" in res.symbols["BTC/USD"]


def test_daily_loss_breaker_blocks_new_limit(dip_world):
    bars, ticks, _ = dip_world
    from dublin_bot.state import StateStore
    from dublin_bot.risk import SessionState
    StateStore(Path("logs/session_state.json")).save(
        SessionState(500.0, 500.0, 500.0, realized_pnl_today=-20.0))
    res = _sleeve(_settings(), FakeGateway(bars, ticks)).run_cycle()
    assert "Daily loss circuit breaker" in res.symbols["BTC/USD"]


def test_learner_bench_for_regime_sleeve_does_not_bench_meanrev(tmp_path):
    reg = LearningAgent(tmp_path / "l.json", strategy_key="regime@60m", min_sample=2)
    for _ in range(3):
        reg.record_trade("BTC/USD", -1.0, "chop", notional=100.0)
    assert reg.gate("BTC/USD", now=1e9).allow is False
    mr = LearningAgent(tmp_path / "l.json", strategy_key="meanrev_mk@240m", min_sample=2)
    assert mr.gate("BTC/USD", now=1e9 + 1).allow is True
    assert LearningAgent(tmp_path / "l.json", strategy_key="regime@60m", min_sample=2).gate(
        "BTC/USD", now=1e9 + 2).allow is False


def test_legacy_unscoped_bench_keeps_applying_to_its_strategy(tmp_path):
    p = tmp_path / "l.json"
    p.write_text(json.dumps({"strategy_key": "regime@60m",
                             "benches": {"ETH/USD": {"until": 2e9, "since": 1e9,
                                                     "trades_at_bench": 30, "reason": "old"}}}))
    assert LearningAgent(p, strategy_key="regime@60m").gate("ETH/USD", now=1.5e9).allow is False
    assert LearningAgent(p, strategy_key="meanrev_mk@240m").gate("ETH/USD", now=1.5e9).allow is True


# ── primary engine honours the registry ────────────────────────

def _bare_engine(settings, bot_qty):
    from dublin_bot.engine import TradingEngine

    eng = TradingEngine.__new__(TradingEngine)
    eng.settings = settings
    eng._bot_qty = dict(bot_qty)
    return eng


def test_primary_engine_treats_meanrev_lots_and_orders_as_foreign():
    Path("logs").mkdir(exist_ok=True)
    reg = SleeveRegistry()
    reg.set_sleeve(SLEEVE, owned={"ETH/USD"}, pending={"SOL/USD": 100.0})
    reg.save()
    eng = _bare_engine(_settings(), {"ETH/USD": 0.05, "BTC/USD": 0.001})
    assert eng._foreign_symbols() == {"ETH/USD", "SOL/USD"}
    # BTC (primary) + ETH (meanrev lot) + SOL (meanrev pending) = 3 → cap reached.
    assert eng._open_bot_position_count() == 3


def test_primary_select_never_exits_or_enters_foreign_symbols():
    Path("logs").mkdir(exist_ok=True)
    reg = SleeveRegistry()
    reg.set_sleeve(SLEEVE, owned={"BTC/USD"}, pending={})
    reg.save()
    s = _settings(breakout_symbols=["BTC/USD", "ETH/USD", "SOL/USD"], symbol="BTC/USD")
    eng = _bare_engine(s, {"BTC/USD": 0.001})
    evaluated: list[tuple[str, bool]] = []

    class Strat:
        def evaluate(self, bars, in_position=False):
            evaluated.append((s.symbol, in_position))
            from dublin_bot.models import Signal
            return Signal(Action.WAIT, 0, "wait", 1.0)

    class Gw:
        def get_bars(self):
            return _bars("up")

        def size_buy(self, notional, price=None):
            return SimpleNamespace(volume=1.0)

    eng.strategy, eng.gateway = Strat(), Gw()
    eng.audit = SimpleNamespace(record=lambda *a, **k: None)
    eng.paper_portfolio = PaperPortfolio(Path("logs/paper_portfolio.json"))
    eng.sentiment = SimpleNamespace(should_block_buy=lambda sym: False)
    eng._select_symbol_breakout()
    assert ("BTC/USD", True) not in evaluated          # never managed the meanrev lot
    assert all(sym != "BTC/USD" for sym, _ in evaluated)  # nor scanned it for entry
    assert s.symbol != "BTC/USD"

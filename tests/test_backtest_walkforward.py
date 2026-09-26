"""Fee-aware backtest core + regime sleeve parity (offline, synthetic bars)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from dublin_bot.backtest_core import (
    Costs, Spec, add_indicators, classify_adx_regime, metrics, regime_signals,
    simulate, walk_forward,
)


def _frame(closes, *, spread=0.002, vol=None):
    closes = np.asarray(closes, float)
    n = len(closes)
    opens = np.r_[closes[0], closes[:-1]]
    df = pd.DataFrame({
        "time": np.arange(n) * 3600,
        "open": opens,
        "high": np.maximum(opens, closes) * (1 + spread),
        "low": np.minimum(opens, closes) * (1 - spread),
        "close": closes,
        "volume": np.full(n, 100.0) if vol is None else vol,
    })
    return df


def test_costs_round_trip_on_flat_price():
    c = Costs(fee_bps=40, slippage_bps=5)
    r = c.net_return(100.0, 100.0)
    assert -0.0091 < r < -0.0089  # ≈ -90 bps round trip


def test_breakout_fills_next_open_and_stop_first():
    closes = [100.0] * 260 + [103.0, 103.0, 103.0]
    vol = np.full(263, 100.0)
    vol[260] = 500.0
    df = _frame(closes, vol=vol)
    # bar 261: both +4% TP and -2% stop inside the bar → stop must win
    df.loc[261, "open"] = 103.0
    df.loc[261, "high"] = 103.0 * 1.05
    df.loc[261, "low"] = 103.0 * 0.97
    d = add_indicators(df)
    trades = simulate(d, Spec("breakout", {"lookback": 20, "stop": 0.02, "tp": 0.04}), Costs())
    assert len(trades) == 1
    t = trades[0]
    assert t.entry_i == 261 and t.entry_px == 103.0   # next bar open, not signal close
    assert t.reason == "stop"
    assert t.net < t.gross < 0


def test_metrics_drawdown_and_expectancy():
    closes = [100.0] * 260 + [103.0] * 3
    vol = np.full(263, 100.0)
    vol[260] = 500.0
    d = add_indicators(_frame(closes, vol=vol))
    trades = simulate(d, Spec("breakout", {"lookback": 20, "stop": 0.02, "tp": 0.04}), Costs())
    m = metrics(trades, equity=500)
    assert m.trades == len(trades)
    assert m.max_dd_pct >= 0


def test_walk_forward_attributes_trades_to_segments():
    rng = np.random.default_rng(7)
    closes = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, 2000)))
    vol = rng.uniform(50, 150, 2000)
    d = add_indicators(_frame(closes, vol=vol))
    specs = [Spec("breakout", {"lookback": 20, "stop": 0.02, "tp": 0.04}),
             Spec("breakout", {"lookback": 55, "stop": 0.03, "tp": 0.06})]
    wf = walk_forward(d, specs, Costs(), folds=4)
    assert len(wf.chosen) == 4
    usable = len(d) - 210
    first_oos = 210 + usable // 5
    assert all(t.entry_i >= first_oos for t in wf.oos_trades)
    assert all(t.entry_i >= 210 for t in wf.is_trades)


def _trend_then_chop(n_trend=400, n_chop=200):
    up = 100 * np.exp(np.linspace(0, 0.8, n_trend))
    rng = np.random.default_rng(3)
    chop = up[-1] * (1 + rng.normal(0, 0.002, n_chop))
    return np.r_[up, chop]


def test_regime_strategy_matches_backtest_signals():
    from dublin_bot.config import Settings
    from dublin_bot.models import Action
    from dublin_bot.strategy import build_strategy

    closes = _trend_then_chop()
    df = _frame(closes)
    bars = df.drop(columns=["time"])
    bars.index = pd.date_range("2026-01-01", periods=len(bars), freq="h", tz="UTC")
    s = Settings(_env_file=None, strategy="regime_trend", stop_loss_pct=0.03,
                 take_profit_pct=0.25, regime_lookback=20, regime_atr_mult=3.0)
    strat = build_strategy(s)
    assert type(strat).__name__ == "RegimeTrendStrategy"
    d = add_indicators(bars)
    sig = regime_signals(d, strat.params())
    # check parity on a handful of bars in both regimes
    for end in (300, 350, 399, 500, 599):
        out = strat.evaluate(bars.iloc[: end + 1], in_position=False)
        sub = regime_signals(add_indicators(bars.iloc[: end + 1]), strat.params())
        assert (out.action is Action.BUY) == bool(sub["entry"][-1])
    # trending section should produce at least one entry, chop none
    assert sig["entry"][260:400].any()
    assert not sig["entry"][450:].any()
    # in chop the held position is told to go flat
    out = strat.evaluate(bars, in_position=True)
    assert out.action is Action.SELL


def test_classify_adx_regime_labels():
    bars = _frame(_trend_then_chop()).drop(columns=["time"])
    assert classify_adx_regime(bars.iloc[:400]) == "trend"
    assert classify_adx_regime(bars) in ("chop", "volatile_chop")
    assert classify_adx_regime(bars.iloc[:10]) == "unknown"


def test_maker_cost_cheaper_than_taker():
    c = Costs(fee_bps=40, slippage_bps=5, maker_bps=25)
    taker = c.net_return(100, 100)
    maker_in = c.net_return(100, 100, entry_maker=True)
    both = c.net_return(100, 100, entry_maker=True, exit_maker=True)
    assert taker < maker_in < both < 0
    assert abs(both - (-0.005)) < 2e-4  # ≈ 2 × 25 bps


def test_limit_entry_skips_when_not_filled():
    closes = [100.0] * 260 + [103.0, 104.0, 104.0]
    vol = np.full(263, 100.0)
    vol[260] = 500.0
    df = _frame(closes, vol=vol, spread=0.0)
    df.loc[261, ["open", "low"]] = [103.5, 103.5]  # never trades back to the limit
    d = add_indicators(df)
    mk = Spec("breakout", {"lookback": 20, "stop": 0.02, "tp": 0.04, "entry": "limit"})
    assert simulate(d, mk, Costs()) == [] or all(t.entry_i != 261 for t in simulate(d, mk, Costs()))


def test_meanrev_family_trades_and_reverts():
    rng = np.random.default_rng(11)
    closes = 100 + np.cumsum(rng.normal(0, 0.6, 1500))
    closes = np.abs(closes) + 20
    d = add_indicators(_frame(closes))
    trades = simulate(d, Spec("meanrev", {"rsi_os": 38.0, "rsi_exit": 55.0, "stop": 0.05,
                                          "tp": 0.25}), Costs())
    assert trades and any(t.reason == "reverted" for t in trades)

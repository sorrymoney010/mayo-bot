import pandas as pd

from arc_bot.backtest import monte_carlo_pass, run, stats
from arc_bot.data import synthetic
from arc_bot.rules import MES, PropGuard, PropRules


def test_trailing_floor_and_lock():
    g = PropGuard(PropRules())
    assert g.floor == 24_200
    g.realize(500); g.end_day()
    assert g.floor == 24_700          # trails the EOD peak
    g.realize(900); g.end_day()
    assert g.floor == 25_000          # locks at start balance


def test_intraday_breach_blows():
    g = PropGuard(PropRules())
    g.mark_equity(-801)
    assert g.blown and not g.can_trade()


def test_daily_stop_and_target_halt_trading():
    g = PropGuard(PropRules(), own_daily_stop=300, own_daily_target=450)
    g.realize(-300); assert not g.can_trade()
    g2 = PropGuard(PropRules()); g2.realize(450); assert not g2.can_trade()


def test_risk_budget_never_exceeds_half_cushion():
    g = PropGuard(PropRules())
    g.realize(-250)
    assert g.max_risk_now(500) <= (g.cushion() * 0.5)


def test_consistency_rule():
    g = PropGuard(PropRules(profit_target=1000))
    for p in (600, 200, 200, 150):
        g.realize(p); g.end_day()
    assert g.total_profit() == 1150 and g.consistency_ok() is False  # 600 > 50% of 1150
    g2 = PropGuard(PropRules(profit_target=1000))
    for p in (400, 400, 300):
        g2.realize(p); g2.end_day()
    assert g2.passed()


def test_backtest_runs_and_respects_limits():
    bars = synthetic(80, seed=3)
    res = run(bars, MES)
    s = stats(res)
    assert s["trades"] >= 0
    assert all(t.qty <= 60 for t in res.trades)
    # no day loses more than the firm daily limit
    assert all(d > -1500 for d in res.day_pnls)


def test_monte_carlo_shape():
    out = monte_carlo_pass([150, -100, 200, -80, 120] * 6, n=300)
    assert 0 <= out["p_pass"] <= 1 and 0 <= out["p_blow"] <= 1

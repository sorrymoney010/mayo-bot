
import numpy as np
import pandas as pd

from dublin_bot import perp_research as pr


def _frame(prices, rate=0.0):
    n = len(prices)
    p = np.asarray(prices, float)
    return pd.DataFrame({"time": np.arange(n) * 3600, "open": p, "high": p, "low": p, "close": p,
                         "volume": 1.0, "m_open": p, "m_high": p, "m_low": p, "m_close": p,
                         "s_open": p, "s_close": p, "rate": rate})


def test_funding_sign_and_fees():
    d = _frame([100.0] * 10, rate=1e-4)
    n = len(d)
    le = np.zeros(n, bool)
    le[0] = True
    se = np.zeros(n, bool)
    lx = np.zeros(n, bool)
    lx[4] = True
    tl = pr.simulate(d, {"long_entry": le, "short_entry": se, "long_exit": lx}, symbol="X", lev=1,
                     costs=pr.Costs(5, 2), warm=0)[0]
    # held bars 1..4 -> 4 hours of +1e-4 paid by the long; fees 2 x 7 bps
    assert abs(tl.funding + 4e-4) < 1e-12
    assert abs(tl.net - (-4e-4 - 14e-4)) < 1e-12
    ts = pr.simulate(d, {"long_entry": se, "short_entry": le, "short_exit": lx}, symbol="X", lev=1,
                     costs=pr.Costs(5, 2), warm=0)[0]
    assert ts.funding > 0 and ts.side == -1


def test_liquidation_at_2x_caps_loss():
    d = _frame([100, 100, 120, 160, 170, 170])
    n = len(d)
    se = np.zeros(n, bool)
    se[0] = True
    tr = pr.simulate(d, {"long_entry": np.zeros(n, bool), "short_entry": se}, symbol="X", lev=2,
                     costs=pr.Costs(), warm=0)[0]
    assert tr.reason.startswith("liquidation") and tr.net == -0.5


def test_no_lookahead_daily():
    # daily close of day D is only usable by bars closing at/after D+1 00:00
    days = 80
    hourly = _frame(np.r_[np.linspace(100, 200, days * 24)])
    hourly["time"] = np.arange(days * 24) * 3600
    d = pr.attach_daily(hourly, pr.daily_from_hourly(hourly), 60, now=days * 86400)
    i = 60 * 24 + 5  # 05:00 on day 60
    ct = d["d1_close_time"].iloc[i]
    assert ct <= d["time"].iloc[i] + 3600

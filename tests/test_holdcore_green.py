"""Hold-core signal and the retired-sleeve green bar."""
from __future__ import annotations

import numpy as np
import pandas as pd

from dublin_bot.backtest_core import holdcore_signals
from dublin_bot.promotion import check_strategy, is_retired


def test_holdcore_enters_and_exits_only_on_d1_flip():
    n = 8
    d = pd.DataFrame({"d1_riskon": [0, 0, 1, 1, 1, 0, 0, 1]})
    sig = holdcore_signals(d, {"d1": True, "min_bars": 0})
    assert list(sig["entry"].astype(int)) == [0, 0, 1, 0, 0, 0, 0, 1]
    assert list(sig["exit"].astype(int)) == [0, 0, 0, 0, 0, 1, 0, 0]


def test_retired_sleeves_cannot_pass():
    rows = [{"net_bps": 100, "pnl": 1.0}] * 30
    assert is_retired("breakout@15m")
    assert is_retired("momentum")
    assert is_retired("futures_short@240m")
    assert not is_retired("meanrev_mk@240m")
    assert not is_retired("hold_core")
    blocked = check_strategy(rows, sleeve="breakout@15m")
    assert blocked["passes"] is False
    assert "not_retired" in blocked["failed"]


def test_green_requires_beating_hold_when_benchmark_supplied():
    rows = [{"net_bps": 80, "pnl": 1.0}] * 30
    lost = check_strategy(rows, sleeve="hold_core", benchmark_return_pct=40.0, seed=500)
    assert lost["passes"] is False
    assert "beats_hold" in lost["failed"]
    beat = check_strategy(rows, sleeve="hold_core", benchmark_return_pct=1.0, seed=500)
    assert beat["checks"]["beats_hold"]["pass"] is True

import importlib.util
from pathlib import Path

import pandas as pd

P = Path(__file__).resolve().parents[1] / "tools/daytrading/analyze_trades.py"
spec = importlib.util.spec_from_file_location("analyze_trades", P)
at = importlib.util.module_from_spec(spec)
spec.loader.exec_module(at)


def _csv(tmp_path, rows):
    p = tmp_path / "t.csv"
    pd.DataFrame(rows, columns=["Symbol", "Entry Date", "Entry Time", "Entry Price", "Exit Date",
                                "Exit Time", "Exit Price", "Shares", "P&L", "Setup Type"]).to_csv(p, index=False)
    return str(p)


def test_stats_blocks_and_flags(tmp_path):
    rows = []
    for i in range(6):   # 9:30 block ORB: 4 winners of +100, 2 losers of -50
        rows.append(["TSLA", f"2026-11-0{2 + i % 5}", "09:35", 100, f"2026-11-0{2 + i % 5}", "09:50", 101, 100,
                     100 if i < 4 else -50, "ORB"])
    for i in range(6):   # 2:30 block VWAP: all losers
        rows.append(["NVDA", "2026-11-03", "14:40", 100, "2026-11-03", "15:00", 99, 100, -100, "VWAP Bounce"])
    df = at.load(_csv(tmp_path, rows))
    s = at.stats(df[df["setup type"] == "ORB"])
    assert s["Trades"] == 6 and s["WinRate"] == "67%" and s["TotalPnL"] == 300.0
    assert s["Expectancy"] == round(4 / 6 * 100 + 2 / 6 * -50, 2)
    assert str(df.loc[df["setup type"] == "VWAP Bounce", "block"].iloc[0]) == "2:30-4:00"
    flags = at.flags(df)
    assert any("VWAP Bounce" in f for f in flags) and any("2:30-4:00" in f for f in flags)
    assert "ORB" not in " ".join(flags)


def test_pnl_computed_when_missing_and_bad_rows_dropped(tmp_path):
    rows = [["AMD", "2026-11-02", "10:00", 10, "2026-11-02", "10:30", 12, 100, None, "ORB"],
            ["AMD", "2026-11-02", "10:00", 10, "2026-11-02", "10:30", 9, -100, None, "ORB"],   # short, wins
            ["AMD", "not a date", "10:00", 10, "x", "y", 9, 100, 5, "ORB"]]
    df = at.load(_csv(tmp_path, rows))
    assert sorted(df.pnl) == [100.0, 200.0] and len(df) == 2

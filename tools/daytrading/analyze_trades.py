#!/usr/bin/env python
"""Weekly trade-journal analyzer.   Usage:  python analyze_trades.py trades.csv

Required columns (case-insensitive): Symbol, Entry Date, Entry Time, Entry Price, Exit Date,
Exit Time, Exit Price, Shares, P&L.   Optional: Setup Type (you add it by hand).
If P&L is missing/blank it is computed from prices (long if Shares > 0, short if Shares < 0).
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

REQUIRED = ["symbol", "entry date", "entry time", "entry price", "exit date", "exit time",
            "exit price", "shares"]
MIN_FLAG_TRADES = 5          # need this many trades in a bucket before calling it "consistently losing"
BLOCKS = [("9:30-10:30", 570, 630), ("10:30-11:30", 630, 690), ("11:30-12:30", 690, 750),
          ("12:30-1:30", 750, 810), ("1:30-2:30", 810, 870), ("2:30-4:00", 870, 960)]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]


# ───────────────────────── loading & cleaning ─────────────────────────
def load(path: str) -> pd.DataFrame:
    try:
        df = pd.read_csv(path)
    except FileNotFoundError:
        sys.exit(f"File not found: {path}")
    df.columns = [c.strip().lower().replace("_", " ") for c in df.columns]
    df = df.rename(columns={"p&l": "pnl", "pnl": "pnl", "p/l": "pnl", "profit/loss": "pnl",
                            "setup": "setup type", "setup_type": "setup type"})
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        sys.exit(f"Missing required column(s): {missing}\nFound: {list(df.columns)}")
    n0 = len(df)
    for c in ("entry price", "exit price", "shares"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if "pnl" in df:
        df["pnl"] = pd.to_numeric(df["pnl"].astype(str).str.replace(r"[$,()]", "", regex=True)
                                  .where(~df["pnl"].astype(str).str.startswith("("),
                                         "-" + df["pnl"].astype(str).str.replace(r"[$,()]", "", regex=True)),
                                  errors="coerce")
    else:
        df["pnl"] = np.nan
    calc = (df["exit price"] - df["entry price"]) * df["shares"]      # signed shares => shorts work
    df["pnl"] = df["pnl"].fillna(calc)
    df["entry_dt"] = pd.to_datetime(df["entry date"].astype(str) + " " + df["entry time"].astype(str),
                                    errors="coerce")
    df = df.dropna(subset=["pnl", "entry_dt"]).copy()
    if len(df) < n0:
        print(f"Note: dropped {n0 - len(df)} row(s) with missing/invalid P&L or entry date/time.")
    if df.empty:
        sys.exit("No usable trades after cleaning.")
    if "setup type" not in df:
        print("Note: no 'Setup Type' column; setup analysis skipped.")
        df["setup type"] = "(untagged)"
    df["setup type"] = df["setup type"].fillna("(untagged)").astype(str).str.strip()
    mins = df["entry_dt"].dt.hour * 60 + df["entry_dt"].dt.minute
    df["block"] = pd.cut(mins, [b[1] for b in BLOCKS] + [BLOCKS[-1][2]], right=False,
                         labels=[b[0] for b in BLOCKS])
    df["dow"] = df["entry_dt"].dt.day_name()
    df["win"] = df["pnl"] > 0
    return df


# ───────────────────────── metrics ─────────────────────────
def stats(g: pd.DataFrame) -> dict:
    n = len(g)
    if n == 0:
        return dict(Trades=0, WinRate="-", AvgWin="-", AvgLoss="-", Expectancy="-", TotalPnL=0.0)
    w, l = g.loc[g.pnl > 0, "pnl"], g.loc[g.pnl <= 0, "pnl"]
    wr = len(w) / n
    aw, al = (w.mean() if len(w) else 0.0), (l.mean() if len(l) else 0.0)
    return dict(Trades=n, WinRate=f"{wr:.0%}", AvgWin=round(aw, 2), AvgLoss=round(al, 2),
                Expectancy=round(wr * aw + (1 - wr) * al, 2), TotalPnL=round(g.pnl.sum(), 2))


def table(df: pd.DataFrame, by: str, order=None) -> pd.DataFrame:
    keys = order if order is not None else sorted(df[by].dropna().unique())
    rows = {k: stats(df[df[by] == k]) for k in keys}
    return pd.DataFrame(rows).T


def overall(df: pd.DataFrame) -> pd.Series:
    s = stats(df)
    s.update({"Winners": int(df.win.sum()), "Losers": int((~df.win).sum()),
              "LargestWin": round(df.pnl.max(), 2), "LargestLoss": round(df.pnl.min(), 2),
              "AvgPnL": round(df.pnl.mean(), 2)})
    return pd.Series(s)


# ───────────────────────── patterns & advice ─────────────────────────
def extremes(df: pd.DataFrame, by: str, label: str, out: list[str]) -> None:
    g = df.groupby(by, observed=True).pnl.agg(["sum", "count"])
    g = g[g["count"] >= 1]
    if len(g) < 2:
        return
    best, worst = g["sum"].idxmax(), g["sum"].idxmin()
    out.append(f"Best {label}: {best} (${g.loc[best, 'sum']:,.2f} over {int(g.loc[best, 'count'])} trades)")
    out.append(f"Worst {label}: {worst} (${g.loc[worst, 'sum']:,.2f} over {int(g.loc[worst, 'count'])} trades)")


def flags(df: pd.DataFrame) -> list[str]:
    out = []
    for by, name in (("block", "time block"), ("dow", "weekday"), ("setup type", "setup")):
        for k, g in df.groupby(by, observed=True):
            if len(g) >= MIN_FLAG_TRADES and g.pnl.sum() < 0 and (g.pnl > 0).mean() < 0.45:
                out.append(f"LOSING {name}: {k}  ({len(g)} trades, win {(g.pnl > 0).mean():.0%}, "
                           f"${g.pnl.sum():,.2f})")
    return out


def summary(df: pd.DataFrame) -> str:
    o = overall(df)
    lines = ["", "KEY PATTERNS", "-" * 60]
    pts: list[str] = []
    extremes(df, "block", "hour block", pts)
    extremes(df[df["setup type"] != "(untagged)"], "setup type", "setup", pts)
    extremes(df, "dow", "weekday", pts)
    lines += pts
    fl = flags(df)
    lines += ["", "CONSISTENTLY LOSING (>= %d trades, win rate < 45%%, net negative)" % MIN_FLAG_TRADES,
              "-" * 60] + (fl or ["none flagged"])
    rec = ["", "RECOMMENDATIONS", "-" * 60]
    if o["Expectancy"] != "-" and float(o["Expectancy"]) <= 0:
        rec.append("- Overall expectancy is not positive. Cut size until the leaks below are fixed.")
    for f in fl:
        rec.append(f"- Stop or halve size in: {f.split(':', 1)[1].strip().split('  ')[0]}.")
    aw, al = float(o["AvgWin"] or 0), abs(float(o["AvgLoss"] or 0))
    if al and aw / al < 1.0:
        rec.append(f"- Average win (${aw:.2f}) is smaller than average loss (${al:.2f}): let winners run "
                   "or tighten stops.")
    if len(df) < 30:
        rec.append(f"- Only {len(df)} trades: treat all of this as anecdotal. 30+ per bucket before trusting it.")
    if len(rec) == 3:
        rec.append("- No red flags in this sample. Keep logging.")
    return "\n".join(lines + rec)


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Usage: python analyze_trades.py trades.csv")
    df = load(sys.argv[1])
    pd.set_option("display.width", 140)
    print(f"\nTRADE JOURNAL ANALYSIS: {len(df)} trades, {df.entry_dt.min():%Y-%m-%d} to {df.entry_dt.max():%Y-%m-%d}")
    print("\nOVERALL\n" + "-" * 60 + "\n" + overall(df).to_string())
    print("\nBY SETUP TYPE\n" + "-" * 60 + "\n" + table(df, "setup type").to_string())
    print("\nBY TIME OF DAY (entry time)\n" + "-" * 60 + "\n"
          + table(df, "block", [b[0] for b in BLOCKS]).to_string())
    print("\nBY DAY OF WEEK\n" + "-" * 60 + "\n" + table(df, "dow", DAYS).to_string())
    print(summary(df))


if __name__ == "__main__":
    main()

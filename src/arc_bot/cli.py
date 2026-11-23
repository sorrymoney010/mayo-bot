from __future__ import annotations

import argparse
import json

from .backtest import monte_carlo_pass, run, stats
from .data import load_csv, rth, synthetic
from .rules import INSTRUMENTS
from .strategy import ORBParams


def main(argv=None) -> None:
    ap = argparse.ArgumentParser("arc-bot", description="ORB eval research bot (backtest only)")
    ap.add_argument("--csv", help="1-minute bars CSV (timestamp,open,high,low,close,volume)")
    ap.add_argument("--symbol", default="MES", choices=INSTRUMENTS)
    ap.add_argument("--synthetic-days", type=int, default=0, help="plumbing test only")
    ap.add_argument("--risk", type=float, default=120.0)
    ap.add_argument("--target-r", type=float, default=2.0)
    ap.add_argument("--mc", type=int, default=2000, help="Monte Carlo eval attempts")
    a = ap.parse_args(argv)
    if a.csv:
        bars = rth(load_csv(a.csv))
    elif a.synthetic_days:
        bars = synthetic(a.synthetic_days)
        print("WARNING: synthetic data. Results say nothing about real edge.")
    else:
        ap.error("provide --csv or --synthetic-days")
    p = ORBParams(risk_per_trade=a.risk, target_r=a.target_r)
    res = run(bars, INSTRUMENTS[a.symbol], p)
    print(json.dumps(stats(res), indent=2, default=str))
    # MC on the unconstrained daily distribution (re-run without stop-on-blown effects)
    print(json.dumps(monte_carlo_pass(res.day_pnls, n=a.mc), indent=2))


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import json

from .backtest import monte_carlo_pass, run, stats
from .data import load_csv, rth, synthetic
from .rules import INSTRUMENTS
from .strategy import MRParams, ORBParams


def _strategies(risk: float, target_r: float) -> dict:
    return {"orb": ORBParams(risk_per_trade=risk, target_r=target_r),
            "meanrev": MRParams(risk_per_trade=risk)}


def _row(name: str, res, mc: int) -> dict:
    s = stats(res)
    m = monte_carlo_pass(res.day_pnls, n=mc) if mc and len(res.day_pnls) >= 20 else {}
    return dict(strategy=name, trades=s["trades"], net=s["net"], win=s["win_rate"],
                pf=s["profit_factor"], exp=s["expectancy"], worst_day=s["worst_day"],
                blown=s["blown"], passed=s["passed"],
                p_pass=round(m.get("p_pass", float("nan")), 3),
                p_blow=round(m.get("p_blow", float("nan")), 3))


def _print(rows: list[dict], title: str) -> None:
    print(f"\n== {title} ==")
    cols = list(rows[0])
    print("  ".join(f"{c:>9}" for c in cols))
    for r in rows:
        print("  ".join(f"{str(r[c]):>9}" for c in cols))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser("arc-bot", description="Eval research bot (backtest only)")
    ap.add_argument("--csv", help="1-minute bars CSV (timestamp,open,high,low,close,volume)")
    ap.add_argument("--symbol", default="MES", choices=INSTRUMENTS)
    ap.add_argument("--synthetic-days", type=int, default=0, help="plumbing test only")
    ap.add_argument("--regimes", action="store_true", help="compare on walk/revert/trend synthetic data")
    ap.add_argument("--strategy", default="compare", choices=["orb", "meanrev", "compare"])
    ap.add_argument("--risk", type=float, default=120.0)
    ap.add_argument("--target-r", type=float, default=2.0)
    ap.add_argument("--oos-split", type=float, default=0.6, help="fraction used as in-sample")
    ap.add_argument("--mc", type=int, default=2000, help="Monte Carlo eval attempts")
    a = ap.parse_args(argv)
    inst = INSTRUMENTS[a.symbol]
    strats = _strategies(a.risk, a.target_r)
    chosen = strats if a.strategy == "compare" else {a.strategy: strats[a.strategy]}

    if a.regimes:
        print("WARNING: synthetic regimes. Shows regime sensitivity, NOT real edge.")
        for mode in ("walk", "revert", "trend"):
            bars = synthetic(a.synthetic_days or 250, mode=mode)
            _print([_row(n, run(bars, inst, p), a.mc) for n, p in chosen.items()], f"synthetic:{mode}")
        return
    if a.csv:
        bars = rth(load_csv(a.csv))
    elif a.synthetic_days:
        bars = synthetic(a.synthetic_days)
        print("WARNING: synthetic data. Results say nothing about real edge.")
    else:
        ap.error("provide --csv, --synthetic-days or --regimes")
    days = sorted(set(bars.index.date))
    cut = days[int(len(days) * a.oos_split)]
    for label, part in (("in-sample", bars[bars.index.date < cut]),
                        ("OUT-OF-SAMPLE", bars[bars.index.date >= cut])):
        _print([_row(n, run(part, inst, p), a.mc) for n, p in chosen.items()], label)


if __name__ == "__main__":
    main()

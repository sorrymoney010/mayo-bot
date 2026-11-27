#!/usr/bin/env python
"""Is the 4h regime edge real? Out-of-sample robustness study on deep Binance.us history.

    python scripts/study_binance_regime.py --fee-bps 10 --slip-bps 5 --maker-bps 10
Reports per-symbol and per-year OOS results, a bootstrap CI, and dependence on the best trades.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from dublin_bot.backtest_core import Costs, add_indicators, default_grid, walk_forward  # noqa: E402


def load(sym: str, tf: int, d: Path) -> pd.DataFrame:
    df = pd.read_csv(d / f"{sym}USD_{tf}m.csv")
    return df.sort_values("time").reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(ROOT / "data/binance_us"))
    ap.add_argument("--tf", type=int, default=240)
    ap.add_argument("--family", default="regime")
    ap.add_argument("--symbols", nargs="*", default=["BTC", "ETH", "SOL"])
    ap.add_argument("--fee-bps", type=float, default=10.0)
    ap.add_argument("--slip-bps", type=float, default=5.0)
    ap.add_argument("--maker-bps", type=float, default=10.0)
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--equity", type=float, default=500.0)
    ap.add_argument("--risk", type=float, default=0.01)
    ap.add_argument("--cap", type=float, default=0.40)
    a = ap.parse_args()
    costs = Costs(fee_bps=a.fee_bps, slippage_bps=a.slip_bps, maker_bps=a.maker_bps)
    specs = default_grid()[a.family]
    rows = []
    for sym in a.symbols:
        d = add_indicators(load(sym, a.tf, Path(a.dir)))
        wf = walk_forward(d, specs, costs, folds=a.folds)
        for t in wf.oos_trades:
            frac = min(a.risk / max(t.stop_frac, 1e-9), a.cap)      # position size as fraction of equity
            rows.append(dict(sym=sym, t=pd.to_datetime(t.entry_time, unit="s"), net=t.net * 1e4,
                             pnl_frac=frac * t.net, reason=t.reason))
    df = pd.DataFrame(rows).sort_values("t")
    n = len(df)
    print(f"\n{a.family} {a.tf}m  costs {a.fee_bps}+{a.slip_bps}bps/side  OOS trades={n}")
    print(f"mean net {df.net.mean():.1f} bps  median {df.net.median():.1f}  win {np.mean(df.net > 0):.1%}")
    w, l = df.net[df.net > 0].sum(), -df.net[df.net <= 0].sum()
    print(f"profit factor {w / l:.2f}")
    rng = np.random.default_rng(0)
    bs = [rng.choice(df.net.values, n).mean() for _ in range(5000)]
    lo, hi = np.percentile(bs, [2.5, 97.5])
    print(f"bootstrap 95% CI of mean net: [{lo:.1f}, {hi:.1f}] bps   P(mean<=0)={np.mean(np.array(bs) <= 0):.1%}")
    srt = df.net.sort_values(ascending=False)
    for k in (3, 5, 10):
        print(f"mean ex top-{k} trades: {srt.iloc[k:].mean():.1f} bps")
    print("\nper symbol:")
    print(df.groupby("sym").net.agg(n="count", mean="mean", win=lambda s: (s > 0).mean()).round(2).to_string())
    print("\nper year:")
    print(df.groupby(df.t.dt.year).net.agg(n="count", mean="mean", win=lambda s: (s > 0).mean()).round(2).to_string())
    yrs = (df.t.iloc[-1] - df.t.iloc[0]).days / 365.25
    # Portfolio-style estimate: sum of per-trade equity fractions (ignores overlap/compounding).
    tot = df.pnl_frac.sum()
    print(f"\nOOS span {yrs:.1f}y, {n / yrs:.0f} trades/yr; sum of per-trade equity returns {tot:.1%} "
          f"=> ~{tot / yrs:.1%}/yr on capital (1% risk, {a.cap:.0%} cap, no compounding)")
    print(f"=> on ${a.equity:.0f}: ~${a.equity * tot / yrs:.0f}/yr ;  on $25: ~${25 * tot / yrs:.2f}/yr")
    eq = (1 + df.pnl_frac).cumprod()
    print(f"worst closed-trade drawdown: {((eq / eq.cummax()) - 1).min():.1%}")


if __name__ == "__main__":
    main()

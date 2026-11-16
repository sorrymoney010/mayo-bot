"""Walk-forward CLI. Default target: BTC/USD. No orders.

    PYTHONPATH=src:. python scripts/walk_forward.py --symbol BTC/USD --days 365 --interval 60
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from dublin_bot.config import Settings
from dublin_bot.engine import build_gateway
from dublin_bot.ohlc import cache_path, load_or_fetch
from dublin_bot.walk_forward import run_walk_forward, write_deploy


def main() -> None:
    ap = argparse.ArgumentParser(description="Walk-forward optimizer (stitched OOS curve)")
    ap.add_argument("--symbol", default="BTC/USD")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--interval", type=int, default=60, help="minutes")
    ap.add_argument("--equity", type=float, default=1000.0)
    ap.add_argument("--is-days", type=int, default=45)
    ap.add_argument("--oos-days", type=int, default=14)
    ap.add_argument("--refresh", action="store_true", help="ignore OHLC cache")
    ap.add_argument("--out", default="logs/wfo_report.json")
    args = ap.parse_args()

    settings = Settings()
    settings.symbol = args.symbol
    settings.timeframe_minutes = args.interval
    gw = build_gateway(settings)
    raw = load_or_fetch(gw, args.symbol, args.days, args.interval, refresh=args.refresh)
    if raw.empty:
        raise SystemExit(f"No OHLC for {args.symbol}")
    print(f"ohlc bars={len(raw)} cache={cache_path(args.symbol, args.interval)}")

    bars = raw.copy()
    if "time" in bars.columns:
        bars.index = pd.to_datetime(bars["time"].astype(float), unit="s", utc=True)
    bars = bars[["open", "high", "low", "close", "volume"]].astype(float)

    bars_per_day = max(int(24 * 60 / args.interval), 1)
    report = run_walk_forward(
        bars,
        settings,
        is_bars=args.is_days * bars_per_day,
        oos_bars=args.oos_days * bars_per_day,
        step_bars=args.oos_days * bars_per_day,
        start_equity=args.equity,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    deploy = write_deploy(report)
    safe = args.symbol.replace("/", "-")
    tagged = write_deploy(report, Path(f"logs/wfo_deploy_{safe}.json"))
    h = report["holdout"]
    print("target bot      : 70815a54-2ca8-4b85-9474-d90be603bcbd")
    print(f"symbol          : {args.symbol}")
    print(f"folds           : {report['n_folds']}")
    print(f"OOS trades/fold : {report['oos_trades']}")
    print(f"OOS median n    : {report['oos_median_trades']:.1f}")
    print(f"thin fold frac  : {report['thin_fold_frac']*100:.0f}%")
    print(f"OOS median SR   : {report['oos_median_sharpe']:.2f}")
    print(f"OOS p5 SR       : {report['oos_p5_sharpe']:.2f}")
    print(f"OOS win folds   : {report['oos_win_fold_frac']*100:.0f}%")
    print(f"holdout Sharpe  : {h['sharpe']:.2f}  DD {h['max_dd']*100:.1f}%  trades {h['n_trades']}")
    print(f"deploy params   : {report['deploy_params']}")
    print(f"verdict         : {report['verdict']}")
    print(f"wrote           : {out}  {deploy}  {tagged}")
    if report["verdict"] in {"DO_NOT_DEPLOY", "FOLDS_TOO_THIN"}:
        sys.exit(2)


if __name__ == "__main__":
    main()

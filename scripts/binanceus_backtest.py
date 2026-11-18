#!/usr/bin/env python3
"""Backtest the three sleeves on Binance.US's own price history.

The earlier "edge" numbers came from Kraken prices over ~3-4 months. Binance.US
serves years of klines, so this re-tests the same rules (shared
``backtest_core``) on the venue you would trade, with its fees.

  python scripts/binanceus_backtest.py                       # BTC/ETH/SOL, 2 years
  python scripts/binanceus_backtest.py --days 1095 --refresh # 3 years, re-download
  python scripts/binanceus_backtest.py --symbols BTC/USD VET/USD HBAR/USD

Reports, per sleeve and coin:
  * walk-forward OOS (params picked on the previous fold, scored on the next)
  * the fixed default params over the whole period (what the runner trades)
  * the same under heavier slippage (stress)
  * buy-and-hold over the same period
and a verdict per sleeve: PASS needs >= 30 OOS trades, profit factor >= 1.10,
and a positive result under stress.

Data is cached in data/binanceus_<BASE>_<tf>m.csv.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dublin_bot.alpaca_paper import SLEEVES  # noqa: E402
from dublin_bot.backtest_core import (  # noqa: E402
    Costs, add_indicators, default_grid, metrics, simulate, walk_forward,
)
from dublin_bot.binanceus_gateway import BinanceUSGateway  # noqa: E402
from dublin_bot.config import Settings  # noqa: E402

DATA = ROOT / "data"
FAMILY = {"regime_1h": "regime", "regime_4h_mk": "regime_mk", "meanrev_4h_mk": "meanrev_mk"}


def history(gw: BinanceUSGateway, symbol: str, tf: int, days: int, refresh: bool) -> pd.DataFrame:
    base = symbol.split("/")[0]
    path = DATA / f"binanceus_{base}_{tf}m.csv"
    if path.exists() and not refresh:
        df = pd.read_csv(path)
        span = (df["time"].iloc[-1] - df["time"].iloc[0]) / 86400 if len(df) else 0
        if span >= days * 0.95:
            return df
    bars = int(days * 1440 / tf)
    df = gw.fetch_bars(symbol, tf, bars=bars)
    DATA.mkdir(exist_ok=True)
    df.to_csv(path, index=False)
    return df


def profit_factor(trades) -> float:
    g = sum(t.net for t in trades if t.net > 0)
    loss = -sum(t.net for t in trades if t.net < 0)
    return g / loss if loss > 0 else (float("inf") if g > 0 else 0.0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", nargs="+", default=["BTC/USD", "ETH/USD", "SOL/USD"])
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--slip", type=float, default=5.0, help="slippage bps per taker side")
    ap.add_argument("--stress-slip", type=float, default=20.0)
    args = ap.parse_args()

    s = Settings()
    gw = BinanceUSGateway(s, quote=s.binanceus_quote)
    base_costs = Costs(fee_bps=s.binanceus_taker_fee_bps, slippage_bps=args.slip, maker_bps=s.binanceus_maker_fee_bps)
    stress = Costs(fee_bps=s.binanceus_taker_fee_bps, slippage_bps=args.stress_slip,
                   maker_bps=s.binanceus_maker_fee_bps)
    grid = default_grid()
    rows, agg = [], {k: {"oos": [], "stress": []} for k in SLEEVES}
    print(f"Binance.US backtest  fees {s.binanceus_maker_fee_bps:.0f}/{s.binanceus_taker_fee_bps:.0f} bps  "
          f"slip {args.slip:.0f} (stress {args.stress_slip:.0f})  {args.days} days\n")
    print(f"{'sleeve':14} {'coin':5} {'days':>5} {'OOS n':>6} {'OOS bps':>8} {'OOS PF':>7} "
          f"{'fixed n':>7} {'fixed bps':>9} {'stress bps':>10} {'B&H %':>7}")
    for sleeve, spec in SLEEVES.items():
        fam = FAMILY[sleeve]
        specs = grid[fam]
        for sym in args.symbols:
            try:
                raw = history(gw, sym, spec.tf, args.days, args.refresh)
            except Exception as exc:
                print(f"{sleeve:14} {sym.split('/')[0]:5} download failed: {exc}")
                continue
            if len(raw) < 400:
                print(f"{sleeve:14} {sym.split('/')[0]:5} only {len(raw)} bars, skipped")
                continue
            d = add_indicators(raw)
            days = (raw["time"].iloc[-1] - raw["time"].iloc[0]) / 86400
            wf = walk_forward(d, specs, base_costs, folds=args.folds)
            fixed = simulate(d, specs[0], base_costs)
            fixed_stress = simulate(d, specs[0], stress)
            m_oos, m_fixed, m_st = metrics(wf.oos_trades), metrics(fixed), metrics(fixed_stress)
            bh = (raw["close"].iloc[-1] / raw["close"].iloc[210] - 1) * 100
            agg[sleeve]["oos"].extend(wf.oos_trades)
            agg[sleeve]["stress"].extend(fixed_stress)
            rows.append({"sleeve": sleeve, "symbol": sym, "days": round(days, 1), "bars": len(raw),
                         "oos": m_oos.to_dict(), "fixed": m_fixed.to_dict(), "stress": m_st.to_dict(),
                         "buy_hold_pct": round(bh, 2), "chosen": wf.chosen})
            print(f"{sleeve:14} {sym.split('/')[0]:5} {days:>5.0f} {m_oos.trades:>6} {m_oos.avg_net_bps:>+8.0f} "
                  f"{profit_factor(wf.oos_trades):>7.2f} {m_fixed.trades:>7} {m_fixed.avg_net_bps:>+9.0f} "
                  f"{m_st.avg_net_bps:>+10.0f} {bh:>+7.0f}")

    print("\nVerdict per sleeve (all coins pooled; PASS = OOS n>=30, PF>=1.10, stress > 0):")
    verdict = {}
    for sleeve, a in agg.items():
        oos, st = a["oos"], a["stress"]
        n, pf = len(oos), profit_factor(oos)
        bps = sum(t.net for t in oos) / n * 1e4 if n else 0.0
        st_bps = sum(t.net for t in st) / len(st) * 1e4 if st else 0.0
        ok = n >= 30 and pf >= 1.10 and st_bps > 0
        verdict[sleeve] = {"oos_trades": n, "oos_avg_net_bps": round(bps, 1), "oos_pf": round(pf, 2),
                           "stress_avg_net_bps": round(st_bps, 1), "pass": ok}
        print(f"  {sleeve:14} n={n:<4} avg {bps:+.0f} bps  PF {pf:.2f}  stress {st_bps:+.0f} bps  "
              f"{'PASS' if ok else 'FAIL'}")

    out = DATA / "binanceus_backtest_results.json"
    out.write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                               "days": args.days, "symbols": args.symbols,
                               "costs": {"maker_bps": s.binanceus_maker_fee_bps, "taker_bps": s.binanceus_taker_fee_bps,
                                         "slip_bps": args.slip, "stress_slip_bps": args.stress_slip},
                               "rows": rows, "verdict": verdict}, indent=1))
    print(f"\nsaved {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

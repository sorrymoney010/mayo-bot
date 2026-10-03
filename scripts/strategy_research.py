#!/usr/bin/env python3
"""Weekly walk-forward search. Paper research only — never touches a lock.

Reads cached public bars (data/kraken_<SYM>_<tf>m.csv, same files as
scripts/backtest_walkforward.py). Timeframes under 60 minutes are skipped.
Writes reports/strategy_research_<date>.json and a short markdown twin.
With --register, a candidate that clears the promotion bar is appended to
logs/shadow_sleeves.json (shadow signals, no paper fills).

Example:
    python scripts/strategy_research.py --tfs 60 240
    python scripts/strategy_research.py --tfs 60 240 --register
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dublin_bot.backtest_core import Costs, add_indicators  # noqa: E402
from dublin_bot.research import library, register_shadow, search  # noqa: E402


def _load(symbol: str, tf: int, data: Path):
    import pandas as pd
    base = symbol.replace("/", "")
    parts = []
    for suffix in ("_trades", ""):
        p = data / f"kraken_{base}_{tf}m{suffix}.csv"
        if p.exists():
            df = pd.read_csv(p)
            if len(df):
                parts.append(df)
    if not parts:
        return None
    df = (pd.concat(parts).drop_duplicates("time", keep="last").sort_values("time").reset_index(drop=True))
    gaps = df["time"].diff().fillna(tf * 60) > tf * 60 * 6
    if gaps.any():
        df = df.iloc[int(gaps[gaps].index[-1]):].reset_index(drop=True)
    return add_indicators(df)


def _attach_d1(df, symbol: str, tf: int, data: Path):
    import pandas as pd
    from dublin_bot.daily_filter import attach_d1, riskon_table
    base = symbol.replace("/", "")
    daily = None
    for suffix in ("_trades", ""):
        p = data / f"kraken_{base}_1440m{suffix}.csv"
        if p.exists():
            daily = pd.read_csv(p)
            break
    if daily is None or not len(daily):
        return df
    table = riskon_table(daily)
    return attach_d1(df, table, tf)


def render(rep: dict) -> str:
    lines = [
        f"# Strategy research {rep['generated_at']}",
        "",
        "Paper research only. This run does not change PAPER_TRADING, DRY_RUN,",
        "ALLOW_LIVE_TRADING, the ledger-owner gate, or the single-instance lock.",
        "Momentum and sub-1h entries are not in the library.",
        "",
    ]
    if rep.get("sample"):
        lines.append(rep["sample"])
        lines.append("")
    if rep.get("history") == "missing":
        lines.append("No cached bars were found. Nothing was scored.")
        lines.append("")
        lines.append("```")
        lines.append("python scripts/fetch_kraken_history.py ohlc --tfs 60 240 1440")
        lines.append("python scripts/strategy_research.py --tfs 60 240")
        lines.append("```")
        return "\n".join(lines)
    for row in rep["results"]:
        if row.get("refused"):
            lines.append(f"- {row['timeframe_minutes']}m: {row['refused']}")
            continue
        oos = row["oos"]
        prom = row["promotion"]
        bh = row.get("buy_and_hold") or {}
        lines.append(
            f"- {row['family']}@{row['timeframe_minutes']}m: "
            f"OOS trades {oos['trades']}, mean {oos['avg_net_bps']:.1f} bps, "
            f"return {oos['total_return_pct']:.2f}%, max DD {oos['max_dd_pct']:.2f}%, "
            f"buy-and-hold mean {bh.get('mean_net_bps', float('nan')):.1f} bps, "
            f"promotion {'PASS' if row['passes'] else 'not yet'} "
            f"(failed: {', '.join(prom['failed']) or 'none'})"
        )
    if not rep["winners"]:
        lines.append("")
        lines.append("No candidate cleared the promotion bar. Nothing registered.")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tfs", nargs="*", type=int, default=[60, 240])
    ap.add_argument("--symbols", nargs="*", default=["BTC/USD", "ETH/USD", "SOL/USD"])
    ap.add_argument("--data", default=str(ROOT / "data"))
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--equity", type=float, default=500.0)
    ap.add_argument("--fee-bps", type=float, default=40.0)
    ap.add_argument("--slip-bps", type=float, default=10.0)
    ap.add_argument("--maker-bps", type=float, default=25.0)
    ap.add_argument("--register", action="store_true")
    ap.add_argument("--shadow-path", default=str(ROOT / "logs" / "shadow_sleeves.json"))
    args = ap.parse_args()
    data = Path(args.data)
    frames: dict[int, list] = {}
    found = 0
    for tf in args.tfs:
        bucket = []
        for sym in args.symbols:
            df = _load(sym, tf, data)
            if df is None:
                continue
            df = _attach_d1(df, sym, tf, data)
            bucket.append(df)
            found += 1
        frames[tf] = bucket
    day = time.strftime("%Y-%m-%d")
    out_dir = ROOT / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    costs = Costs(fee_bps=args.fee_bps, slippage_bps=args.slip_bps, maker_bps=args.maker_bps)
    if found == 0:
        rep = {"generated_at": day, "history": "missing", "results": [], "winners": [],
               "library": {k: [s.label() for s in v] for k, v in library().items()}}
    else:
        rep = search(frames, costs, folds=args.folds, equity=args.equity)
        rep["generated_at"] = day
        rep["history"] = "cache"
        rep["sample"] = (
            "Kraken public OHLC is capped near 720 bars "
            "(about 30 days at 1h, 120 days at 4h). This is not a multi-year sample."
        )
        rep["costs"] = {"fee_bps": args.fee_bps, "slip_bps": args.slip_bps, "maker_bps": args.maker_bps}
        rep["library"] = {k: [s.label() for s in v] for k, v in library().items()}
        if args.register:
            rep["registered"] = [register_shadow(w, path=args.shadow_path,
                                                 report=f"reports/strategy_research_{day}.json")
                                 for w in rep["winners"]]
    json_path = out_dir / f"strategy_research_{day}.json"
    md_path = out_dir / f"strategy_research_{day}.md"
    json_path.write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    md_path.write_text(render(rep) + "\n", encoding="utf-8")
    print(md_path.read_text(encoding="utf-8"))
    print(f"wrote {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

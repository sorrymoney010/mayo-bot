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

from dublin_bot.backtest_core import Costs  # noqa: E402
from dublin_bot.research import library, register_shadow, search  # noqa: E402
from dublin_bot.study_data import load_study_bars, unique_report  # noqa: E402


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
    of = rep.get("orderflow")
    if of:
        lines.append("")
        if of.get("report"):
            lines.append(f"Order-flow overlays ({of.get('configs_tried')} pre-registered configs): "
                         f"{', '.join(of.get('shadow_candidates') or []) or 'no shadow candidate'}; "
                         f"see {of['report']}")
        else:
            lines.append(f"Order-flow overlays: {of.get('skipped') or of.get('error')}")
    return "\n".join(lines)


def orderflow_overlays(args, data: Path, costs: Costs, out_dir: Path) -> dict:
    """Order-flow entry filters / exit timers on the live sleeves (needs tick files).

    With --register, overlays that clear the backtest bar AND the improvement gate move
    candidate -> shadow automatically in --flow-registry (no fills, no locks)."""
    if not (data / "ticks").exists():
        return {"skipped": "no tick files in the data dir"}
    try:
        from dublin_bot.flow_promotion import update_from_backtest
        from dublin_bot.flow_research import load_flow_sleeve_frames, render_md, run_study
        frames = load_flow_sleeve_frames(data, args.symbols, cache=args.flow_cache)
        if not frames:
            return {"skipped": "no contiguous tick run"}
        rep = run_study(frames, costs, folds=args.folds)
        paths = unique_report(out_dir, "orderflow_overlays", ".json", ".md")
        paths[".json"].write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
        paths[".md"].write_text(render_md(rep), encoding="utf-8")
        summary = {"report": str(paths[".md"].relative_to(ROOT)) if paths[".md"].is_relative_to(ROOT)
                   else str(paths[".md"]), "configs_tried": rep["configs_tried"],
                   "shadow_candidates": rep["shadow_candidates"]}
        if args.register:
            reg = update_from_backtest(rep, path=args.flow_registry, report_path=summary["report"])
            summary["stages"] = {k: v.get("stage") for k, v in reg.items() if v.get("stage") != "candidate"}
        return summary
    except Exception as exc:  # noqa: BLE001 — the overlay study must not sink the weekly run
        return {"error": f"{type(exc).__name__}: {exc}"}


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
    ap.add_argument("--no-orderflow", action="store_true",
                    help="skip the order-flow overlay study (filters/exit timers on the live sleeves)")
    ap.add_argument("--flow-registry", default=str(ROOT / "logs" / "flow_overlays.json"))
    ap.add_argument("--flow-cache", default="/tmp/mayo-flow-cache",
                    help="per-day 1m flow cache (never inside --data)")
    args = ap.parse_args()
    data = Path(args.data)
    frames: dict[int, list] = {}
    found = 0
    for tf in args.tfs:
        bucket = []
        for sym in args.symbols:
            df, src = load_study_bars(sym, tf, data)
            if df is None:
                continue
            df.attrs["source"] = src
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
        sources = [getattr(df, "attrs", {}).get("source") for frames_ in frames.values() for df in frames_]
        rep["history"] = "ticks" if "ticks" in sources else "ohlc"
        if rep["history"] == "ticks":
            rep["sample"] = "Tick-built bars from the data dir (read-only; no bars1m cache written)."
        else:
            rep["sample"] = (
                "No tick-built bars in the data dir. Fell back to Kraken public OHLC, "
                "which is capped near 720 bars (about 30 days at 1h, 120 days at 4h)."
            )
        rep["costs"] = {"fee_bps": args.fee_bps, "slip_bps": args.slip_bps, "maker_bps": args.maker_bps}
        rep["library"] = {k: [s.label() for s in v] for k, v in library().items()}
    paths = unique_report(out_dir, "strategy_research", ".json", ".md")
    json_path, md_path = paths[".json"], paths[".md"]
    if found and args.register:
        rel = str(json_path.relative_to(ROOT))
        rep["registered"] = [register_shadow(w, path=args.shadow_path, report=rel)
                             for w in rep["winners"]]
    if found and not args.no_orderflow:
        rep["orderflow"] = orderflow_overlays(args, data, costs, out_dir)
    json_path.write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    md_path.write_text(render(rep) + "\n", encoding="utf-8")
    print(md_path.read_text(encoding="utf-8"))
    print(f"wrote {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Order-flow ENTRY FILTERS + EXIT TIMERS on the three paper sleeves (walk-forward, after fees).

Reads Kraken spot ticks READ-ONLY from --data (never writes there). Per-day 1m flow
caches go to --cache (default /tmp, outside the repo data dir).

Sleeves (the live defaults from ``research.library()``):
  regime@60m, meanrev_mk@240m (post-only limit entries), trendhold@240m.
Variants are pre-registered in ``dublin_bot.flow_research.VARIANTS`` (fixed thresholds,
no fitting). Everything is scored on walk-forward OOS folds 1..4 after a 210-bar warm-up
on the LONGEST contiguous tick-covered run.

    python scripts/study_orderflow_exits.py --data data
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
from dublin_bot.flow_research import load_flow_sleeve_frames, render_md, run_study  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "data"))
    ap.add_argument("--cache", default="/tmp/mayo-flow-cache")
    ap.add_argument("--symbols", nargs="*", default=["BTC/USD", "ETH/USD", "SOL/USD"])
    ap.add_argument("--fee-bps", type=float, default=40.0)
    ap.add_argument("--slip-bps", type=float, default=10.0)
    ap.add_argument("--maker-bps", type=float, default=25.0)
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--out-dir", default=str(ROOT / "reports"))
    a = ap.parse_args()
    t0 = time.time()
    costs = Costs(fee_bps=a.fee_bps, slippage_bps=a.slip_bps, maker_bps=a.maker_bps)
    frames = load_flow_sleeve_frames(Path(a.data), a.symbols, cache=a.cache)
    rep = run_study(frames, costs, folds=a.folds)
    rep["runtime_s"] = round(time.time() - t0, 1)
    day = time.strftime("%Y-%m-%d")
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    jp, mp = out / f"orderflow_exits_{day}.json", out / f"orderflow_exits_{day}.md"
    jp.write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    mp.write_text(render_md(rep), encoding="utf-8")
    print(mp.read_text(encoding="utf-8"))
    print(f"wrote {jp} and {mp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

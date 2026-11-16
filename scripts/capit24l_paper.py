#!/usr/bin/env python3
"""CAPIT-24L PAPER runner (main plan: Binance.US, VET/HBAR/ALGO/XLM). PAPER ONLY - no orders,
no keys, no .env, public Binance.US klines only (1h signal, 1m limit-fill window).

  python scripts/capit24l_paper.py --once                     # one cycle, print summary
  python scripts/capit24l_paper.py --loop                     # every hour, ~150 s after each 1h close
  python scripts/capit24l_paper.py --once --out /tmp/x --seed-cursor 2026-11-02T00:00:00Z
                                                              # dry replay into a scratch dir

Start/stop in the background with paper/capit24l_binanceus/start.sh / stop.sh (env -i, nohup).
Runtime output (gitignored): paper/capit24l_binanceus/{status.json,state.json,signals.jsonl,
closed_trades.jsonl,equity.jsonl,events.jsonl,promotion_report.json,run.log,runner.pid}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dublin_bot.capit24l import Capit24LPaper  # noqa: E402
from dublin_bot.wallclock import sleep_until  # noqa: E402

DEFAULT_OUT = ROOT / "paper" / "capit24l_binanceus"


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S %Z") + " " + msg, flush=True)


def summary(st: dict) -> str:
    b = st.get("books", {}).get("binanceus_l", {})
    return (f"cycle ok: last_closed_bar={st.get('last_closed_bar')} cursor={st.get('cursor')} "
            f"open={len(st.get('open_positions', []))} new_signals={len(st.get('new_signals_this_cycle', []))} "
            f"closed_new={st.get('new_closed_trades_this_cycle')} fills={st.get('fill_counts')} "
            f"bus_l n={b.get('trades')} pf={b.get('pf')} equity=${st.get('equity_primary')} "
            f"floor_halt={st.get('floor_halt')} killed={st.get('killed')}")


def one(out: Path, seed_cursor=None) -> dict:
    st = Capit24LPaper(out, seed_cursor=seed_cursor).run_cycle()
    log(summary(st) if st.get("ok") else f"cycle skipped: {st.get('error')}")
    return st


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true")
    g.add_argument("--loop", action="store_true")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--seed-cursor", default=None,
                    help="ONLY for a fresh state dir: decide signal bars after this UTC time (replay)")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    seed = pd.Timestamp(a.seed_cursor) if a.seed_cursor else None
    if seed is not None and seed.tzinfo is None:
        seed = seed.tz_localize("UTC")
    if a.once:
        st = one(out, seed)
        print(json.dumps({k: st.get(k) for k in ("ok", "last_closed_bar", "open_positions", "fill_counts",
                                                   "equity_primary", "killed", "floor_halt")}, default=str))
        return 0 if st.get("ok") else 1
    (out / "runner.pid").write_text(str(os.getpid()))
    log(f"CAPIT-24L paper loop start pid={os.getpid()} out={out} (paper only, no orders)")
    while True:
        try:
            one(out, seed)
        except Exception:
            log("cycle error:\n" + traceback.format_exc())
        now = time.time()
        # ~150 s after the next 1h close: the previous hour's 1m limit window is complete by then
        sleep_until((int(now // 3600) + 1) * 3600 + 150)


if __name__ == "__main__":
    sys.exit(main())

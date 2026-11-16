#!/usr/bin/env python3
"""CAPIT-24 PAPER runner (the one active strategy). PAPER ONLY - no orders, no keys, no .env.

  python scripts/capit_paper.py --once      # one cycle, print status summary
  python scripts/capit_paper.py --loop      # cycle every hour, ~90 s after each 1h bar closes

Start/stop in the background with paper/capit_binanceus/start.sh / stop.sh (env -i, nohup).
Output (runtime, gitignored): paper/capit_binanceus/{status.json,state.json,signals.jsonl,
closed_trades.jsonl,equity.jsonl,events.jsonl,promotion_report.json,run.log}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dublin_bot.capit24 import CapitPaper  # noqa: E402

DEFAULT_OUT = ROOT / "paper" / "capit_binanceus"


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S %Z") + " " + msg, flush=True)


def summary(st: dict) -> str:
    b = st.get("books", {})
    return (f"cycle ok: last_closed_bar={st.get('last_closed_bar')} cursor={st.get('cursor')} "
            f"open={len(st.get('open_positions', []))} new_signals={len(st.get('new_signals_this_cycle', []))} "
            f"closed_new={st.get('new_closed_trades_this_cycle')} "
            f"bus n={b.get('binanceus', {}).get('trades')} pf={b.get('binanceus', {}).get('pf')} "
            f"krk80 n={b.get('kraken80', {}).get('trades')} pf={b.get('kraken80', {}).get('pf')} "
            f"equity_bus=${st.get('equity_binanceus')} killed={st.get('killed')}")


def one(out: Path) -> dict:
    st = CapitPaper(out).run_cycle()
    log(summary(st) if st.get("ok") else f"cycle skipped: {st.get('error')}")
    return st


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true")
    g.add_argument("--loop", action="store_true")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if a.once:
        st = one(out)
        print(json.dumps({k: st.get(k) for k in ("ok", "last_closed_bar", "open_positions", "killed")},
                         default=str))
        return 0 if st.get("ok") else 1
    (out / "runner.pid").write_text(str(os.getpid()))
    log(f"CAPIT-24 paper loop start pid={os.getpid()} out={out} (paper only, no orders)")
    while True:
        try:
            one(out)
        except Exception:
            log("cycle error:\n" + traceback.format_exc())
        now = time.time()
        nxt = (int(now // 3600) + 1) * 3600 + 90       # ~90 s after the next 1h bar closes
        time.sleep(max(30.0, nxt - now))


if __name__ == "__main__":
    sys.exit(main())

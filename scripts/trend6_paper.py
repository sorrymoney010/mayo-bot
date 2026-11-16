#!/usr/bin/env python3
"""TREND-6 PAPER runner (Binance.US, BTC + ETH, daily trend ensemble). PAPER ONLY - no orders,
no keys, no .env. Venue: Binance.US only (public daily klines; Binance global data.binance.vision
archives only to warm up SMA200 across the 2023-07..2025-02 Binance.US gap).

  python scripts/trend6_paper.py --once            # one cycle, print summary
  python scripts/trend6_paper.py --loop            # hourly, ~180 s past each UTC hour; the daily
                                                   # decision lands on the first cycle after 00:00 UTC
  python scripts/trend6_paper.py --once --out /tmp/x   # scratch dir

Catch-up safe: every closed day after the cursor is decided in order and filled at its next open.
Idempotent: a second cycle in the same day changes nothing (cursor + cycle.lock).
Start/stop in the background with paper/trend6_binanceus/start.sh / stop.sh (env -i, nohup).
Runtime output (gitignored): paper/trend6_binanceus/{status.json,state.json,decisions.jsonl,fills.jsonl,
skipped.jsonl,equity.jsonl,events.jsonl,run.log,runner.pid}
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

from dublin_bot.trend6 import Trend6Paper  # noqa: E402
from dublin_bot.wallclock import sleep_until  # noqa: E402

DEFAULT_OUT = ROOT / "paper" / "trend6_binanceus"


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S %Z") + " " + msg, flush=True)


def summary(st: dict) -> str:
    b = st.get("books", {}).get(st.get("primary_book", "binanceus"), {})
    return (f"cycle ok: decided_through={st.get('cursor_decision_day')} new={st.get('new_decisions_this_cycle')} "
            f"targets={st.get('targets')} equity=${st.get('equity_primary')} cash={st.get('cash_pct_primary')} "
            f"bench=${st.get('bench_5050_hold')} rt={b.get('closed_round_trips')} pf={b.get('pf')} "
            f"killed={st.get('killed')}")


def one(out: Path) -> dict:
    st = Trend6Paper(out).run_cycle()
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
        print(json.dumps({k: st.get(k) for k in ("ok", "cursor_decision_day", "targets", "equity_primary",
                                                   "cash_pct_primary", "killed", "error")}, default=str))
        return 0 if st.get("ok") else 1
    (out / "runner.pid").write_text(str(os.getpid()))
    log(f"TREND-6 paper loop start pid={os.getpid()} out={out} (paper only, no orders)")
    while True:
        try:
            one(out)
        except Exception:
            log("cycle error:\n" + traceback.format_exc())
        now = time.time()
        sleep_until((int(now // 3600) + 1) * 3600 + 180)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Inspect / run the SHADOW perp sleeves by hand. Public Kraken Futures data only, no keys,
no orders, no paper-book or learner writes. Only the shadow's own logs/futures_shadow_* files.

    python scripts/futures_shadow.py --sleeve all --status       # rule state now + virtual positions (no writes)
    python scripts/futures_shadow.py --sleeve d1flip --once      # one shadow cycle (writes its own ledger)
    python scripts/futures_shadow.py --sleeve all --promotion    # promotion bar on forward shadow trades
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dublin_bot.futures_costs import SPOT_TO_PERP  # noqa: E402
from dublin_bot.futures_shadow import D1FlipShadow, Donchian4hShadow, ShadowResult  # noqa: E402


def make(name: str, args) -> list:
    kw = {"logs_dir": Path(args.logs_dir), "book_usd": args.book_usd}
    out = []
    if name in ("d1flip", "all"):
        out.append(D1FlipShadow(bear=args.bear, leverage=args.leverage, **kw))
    if name in ("donchian4h", "all"):
        out.append(Donchian4hShadow(entry_days=args.entry_days, exit_days=args.exit_days,
                                    leverage=args.leverage, **kw))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sleeve", choices=["d1flip", "donchian4h", "all"], default="all")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--status", action="store_true")
    g.add_argument("--once", action="store_true")
    g.add_argument("--promotion", action="store_true")
    ap.add_argument("--logs-dir", default=str(ROOT / "logs"))
    ap.add_argument("--book-usd", type=float, default=500.0)
    ap.add_argument("--leverage", type=float, default=1.0)
    ap.add_argument("--bear", choices=["strict", "repo"], default="strict")
    ap.add_argument("--entry-days", type=int, default=55)
    ap.add_argument("--exit-days", type=int, default=20)
    args = ap.parse_args()
    now = time.time()
    for s in make(args.sleeve, args):
        if args.promotion:
            print(s.name, json.dumps(s.promotion(), indent=1))
        elif args.once:
            print(s.run_cycle(force=True).line())
        else:
            st = s.load()
            marks = {}
            for sym in s.symbols:
                perp = SPOT_TO_PERP.get(sym, sym)
                pos = st["positions"].get(perp)
                cur = int(pos["side"]) if pos else 0
                q = s.feed.ticker(perp)
                marks[perp] = q["mark"]
                new, info = s.decide(perp, cur, now)
                print(f"{s.name:<10} {perp:<10} held={cur:+d} rule_now={new:+d} mark={q['mark']:.6g} "
                      f"{json.dumps(info, default=str)}")
            res = ShadowResult(s.name, reason="status (read-only)")
            s._fill_status(res, st, marks)
            print(res.line())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

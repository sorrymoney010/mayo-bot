#!/usr/bin/env python3
"""Re-score stored walk-forward OOS results under a different venue fee schedule.

Uses each result's ``avg_gross_bps`` (pre-cost) and subtracts a round-trip cost.
Read-only: it only reads data/walkforward_results.json and prints a table.

Examples:
  python scripts/rescore_fees.py                                   # Kraken Tier 1 vs Binance.US
  python scripts/rescore_fees.py --taker 2 --maker 0 --slip 15     # Binance.US, heavier slippage

Cost model (per round trip):
  taker families: 2 * (taker + slip)
  *_mk families:  maker entry + (maker or taker+slip) exit, shown as a best..worst range
"""
from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

VENUES = {
    "kraken_t1": {"taker": 80.0, "maker": 40.0, "slip": 5.0},   # Kraken Pro Tier 1, eff. 2026-07-09
    "binance_us": {"taker": 2.0, "maker": 0.0, "slip": 5.0},    # Binance.US, eff. 2026-04-22
}


def round_trip(family: str, taker: float, maker: float, slip: float) -> tuple[float, float]:
    if family.endswith("_mk"):
        best = maker + maker
        worst = maker + taker + slip
        return best, worst
    rt = 2 * (taker + slip)
    return rt, rt


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="data/walkforward_results.json")
    ap.add_argument("--taker", type=float, help="custom taker bps/side")
    ap.add_argument("--maker", type=float, help="custom maker bps/side")
    ap.add_argument("--slip", type=float, default=5.0, help="slippage bps/side on taker fills")
    a = ap.parse_args()

    data = json.loads(Path(a.results).read_text())
    agg: dict[tuple[int, str], list[float]] = collections.defaultdict(lambda: [0, 0.0])
    for r in data["results"]:
        n = r["oos"]["trades"]
        if n:
            k = agg[(r["tf"], r["family"])]
            k[0] += n
            k[1] += r["oos"]["avg_gross_bps"] * n

    venues = dict(VENUES)
    if a.taker is not None or a.maker is not None:
        venues = {"custom": {"taker": a.taker or 0.0, "maker": a.maker or 0.0, "slip": a.slip}}

    print(f"source: {a.results} (generated {data.get('generated_at', '?')})")
    hdr = f"{'tf':>5} {'family':12} {'n':>4} {'gross':>6}"
    for v in venues:
        hdr += f" {v:>14}"
    print(hdr)
    for (tf, fam), (n, g) in sorted(agg.items()):
        g /= n
        line = f"{tf:>5} {fam:12} {int(n):>4} {g:>+6.0f}"
        for c in venues.values():
            best, worst = round_trip(fam, c["taker"], c["maker"], c["slip"])
            cell = f"{g - best:+.0f}" if best == worst else f"{g - worst:+.0f}..{g - best:+.0f}"
            line += f" {cell:>14}"
        print(line)
    print("net bps per trade after costs; positive != proven edge (check n, folds, buy-and-hold)")


if __name__ == "__main__":
    main()

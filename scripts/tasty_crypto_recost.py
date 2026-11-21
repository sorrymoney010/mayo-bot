"""Re-cost the existing walk-forward results at tastytrade crypto fees.

The walk-forward in data/walkforward_results.json records each strategy's
*gross* out-of-sample edge per trade (avg_gross_bps).  Net edge on a venue is
gross minus that venue's round trip.  tastytrade charges a flat commission per
side (0.75% BTC/ETH, 1% others) with no maker discount, so every row is costed
as taker.  Slippage is kept at the walk-forward's 5 bps per side.

Usage:  python scripts/tasty_crypto_recost.py [results.json]
Writes: reports/tasty_crypto_recost.txt
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SLIP_BPS = 5.0
KRAKEN_TAKER_RT = 2 * (40.0 + SLIP_BPS)


def tasty_rt(symbol: str) -> float:
    fee = 75.0 if symbol.split("/")[0] in ("BTC", "ETH") else 100.0
    return 2 * (fee + SLIP_BPS)


def main(path: str = "data/walkforward_results.json") -> int:
    data = json.loads(Path(path).read_text())
    rows = []
    for r in data["results"]:
        oos = r.get("oos") or {}
        n = int(oos.get("trades") or 0)
        if n == 0 or oos.get("avg_gross_bps") is None:
            continue
        gross = float(oos["avg_gross_bps"])
        rows.append({
            "symbol": r["symbol"], "tf": r["tf"], "family": r["family"], "n": n,
            "gross": gross, "kraken": gross - KRAKEN_TAKER_RT,
            "tasty": gross - tasty_rt(r["symbol"]), "tasty_rt": tasty_rt(r["symbol"]),
        })
    rows.sort(key=lambda x: -x["tasty"])
    survivors = [x for x in rows if x["tasty"] > 0 and x["n"] >= 15]
    lines = [
        "tastytrade crypto re-cost of walk-forward OOS results",
        f"source: {path} (generated {data.get('generated_at', '?')})",
        f"round trip: Kraken taker {KRAKEN_TAKER_RT:.0f} bps | tastytrade BTC/ETH "
        f"{tasty_rt('BTC/USD'):.0f} bps, others {tasty_rt('SOL/USD'):.0f} bps",
        "",
        f"{'symbol':9} {'tf':>4} {'family':12} {'n':>4} {'gross':>7} {'kraken':>7} {'tasty':>7}",
    ]
    for x in rows:
        lines.append(f"{x['symbol']:9} {x['tf']:>4} {x['family']:12} {x['n']:>4} "
                     f"{x['gross']:>7.0f} {x['kraken']:>7.0f} {x['tasty']:>7.0f}")
    lines += [
        "",
        f"rows: {len(rows)} | positive at Kraken taker: "
        f"{sum(1 for x in rows if x['kraken'] > 0)} | positive at tastytrade: "
        f"{sum(1 for x in rows if x['tasty'] > 0)} | positive at tastytrade with n>=15: "
        f"{len(survivors)}",
    ]
    for x in survivors:
        lines.append(f"  survivor: {x['symbol']} {x['tf']}m {x['family']} n={x['n']} "
                     f"net {x['tasty']:.0f} bps/trade")
    out = "\n".join(lines)
    Path("reports").mkdir(exist_ok=True)
    Path("reports/tasty_crypto_recost.txt").write_text(out + "\n")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))

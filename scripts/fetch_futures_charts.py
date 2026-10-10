#!/usr/bin/env python3
"""Download PUBLIC Kraken Futures 1h candles (trade / mark / spot=index) and
public hourly funding history into a cache dir. No keys, GET only.

    python scripts/fetch_futures_charts.py --out /workspace/futures-data
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen

PERPS = ("PF_XBTUSD", "PF_ETHUSD", "PF_SOLUSD")
CHART = "https://futures.kraken.com/api/charts/v1/{typ}/{sym}/1h?from={a}&to={b}"
FUND = "https://futures.kraken.com/derivatives/api/v3/historical-funding-rates?symbol={sym}"


def get(url: str):
    for k in range(5):
        try:
            req = Request(url, headers={"User-Agent": "mayo-bot-research/1.0"})
            with urlopen(req, timeout=30) as r:  # noqa: S310 public GET
                return json.loads(r.read().decode())
        except Exception:
            time.sleep(2 + 2 * k)
    raise RuntimeError(url)


def candles(typ: str, sym: str, start: int, end: int) -> list[tuple]:
    rows = {}
    a = start
    step = 30 * 86400
    while a < end:
        b = min(a + step, end)
        for c in get(CHART.format(typ=typ, sym=sym, a=a, b=b)).get("candles", []):
            t = int(c["time"]) // 1000
            rows[t] = (t, float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"]),
                       float(c.get("volume") or 0))
        a = b
        time.sleep(0.25)
    return [rows[k] for k in sorted(rows)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/workspace/futures-data")
    ap.add_argument("--start", type=int, default=1640995200)  # 2022-01-01
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    end = int(time.time()) // 3600 * 3600
    meta = {}
    for sym in PERPS:
        for typ in ("trade", "mark", "spot"):
            rows = candles(typ, sym, args.start, end)
            p = out / f"futures_{sym}_60_{typ}.csv"
            p.write_text("time,open,high,low,close,volume\n" +
                         "".join(",".join(map(str, r)) + "\n" for r in rows))
            meta[f"{sym}_{typ}"] = {"rows": len(rows), "first": rows[0][0] if rows else None,
                                    "last": rows[-1][0] if rows else None}
            print(sym, typ, len(rows), rows[0][0] if rows else None)
        fr = get(FUND.format(sym=sym))["rates"]
        lines = ["time,rate,abs_rate"]
        for r in fr:
            ts = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).timestamp()
            lines.append(f"{int(ts)},{float(r['relativeFundingRate'])},{float(r['fundingRate'])}")
        (out / f"futures_funding_{sym}.csv").write_text("\n".join(lines) + "\n")
        meta[f"{sym}_funding"] = {"rows": len(fr), "first": fr[0]["timestamp"], "last": fr[-1]["timestamp"]}
    meta["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (out / "fetch_meta.json").write_text(json.dumps(meta, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

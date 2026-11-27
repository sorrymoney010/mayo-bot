#!/usr/bin/env python
"""Download deep OHLCV history from Binance.us public REST (no key) into data/binance_us/.

    python scripts/fetch_binance_history.py --symbols BTC ETH SOL --tfs 60 240 --quote USDT

Writes <dir>/<BASE>USD_<tf>m.csv (time=epoch seconds, open,high,low,close,volume) so
scripts/backtest_walkforward.py --cache-dir data/binance_us --prefix "" can read it.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import pandas as pd
import requests

HOST = "https://api.binance.us"
INTERVAL = {15: "15m", 60: "1h", 240: "4h", 1440: "1d"}


def fetch(pair: str, tf: int, start_ms: int, end_ms: int) -> pd.DataFrame:
    rows, cur = [], start_ms
    while cur < end_ms:
        r = requests.get(f"{HOST}/api/v3/klines", timeout=20,
                         params={"symbol": pair, "interval": INTERVAL[tf], "startTime": cur, "limit": 1000})
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        rows += batch
        nxt = int(batch[-1][0]) + tf * 60_000
        if nxt <= cur:
            break
        cur = nxt
        time.sleep(0.15)
    df = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "volume",
                                     "ct", "qv", "n", "tb", "tq", "ig"])
    if df.empty:
        return df
    out = pd.DataFrame({"time": (df["t"].astype("int64") // 1000)})
    for c in ("open", "high", "low", "close", "volume"):
        out[c] = df[c].astype(float)
    # only fully closed bars
    out = out[out["time"] + tf * 60 <= time.time()]
    return out.drop_duplicates("time").sort_values("time").reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", default=["BTC", "ETH", "SOL"])
    ap.add_argument("--tfs", nargs="*", type=int, default=[60, 240])
    ap.add_argument("--quote", default="USDT")
    ap.add_argument("--since", default="2019-09-01")
    ap.add_argument("--out", default="data/binance_us")
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    start = int(pd.Timestamp(a.since, tz="UTC").timestamp() * 1000)
    end = int(time.time() * 1000)
    for base in a.symbols:
        for tf in a.tfs:
            df = fetch(f"{base}{a.quote}", tf, start, end)
            p = out / f"{base}USD_{tf}m.csv"
            df.to_csv(p, index=False)
            span = (f"{pd.to_datetime(df.time.iloc[0], unit='s'):%Y-%m-%d} -> "
                    f"{pd.to_datetime(df.time.iloc[-1], unit='s'):%Y-%m-%d}") if len(df) else "EMPTY"
            print(f"{base}{a.quote} {tf}m: {len(df)} bars  {span}  -> {p}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Fetch + cache Kraken PUBLIC market history into data/ (no keys, read-only).

Two sources, both public:

* ``ohlc``   – /0/public/OHLC. Kraken only returns the most recent ~720 bars
               per interval (15m ≈ 7.5 days, 1h ≈ 30 days, 4h ≈ 120 days).
               Each run MERGES into the cache so history grows over time.
* ``trades`` – /0/public/Trades paged forward from ``--days`` ago and
               aggregated into 15m OHLCV bars. Slow (1000 trades/request,
               ~1 req/s) but gives far deeper 15m/1h history than OHLC.
               Resumable: progress checkpoints to data/.trades_state_<SYM>.json.

Output: data/kraken_<BASE>USD_<tf>m.csv  (columns: time,open,high,low,close,volume)
Trade-built 15m bars go to data/kraken_<BASE>USD_15m_trades.csv and 1h/4h are
resampled from them into *_60m_trades.csv / *_240m_trades.csv.

    .venv/bin/python scripts/fetch_kraken_history.py ohlc
    .venv/bin/python scripts/fetch_kraken_history.py trades --days 60
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
API = "https://api.kraken.com/0/public/"
PAIRS = {"BTC/USD": "XBTUSD", "ETH/USD": "ETHUSD", "SOL/USD": "SOLUSD", "PUMP/USD": "PUMPUSD"}
COLS = ["time", "open", "high", "low", "close", "volume"]


def _get(method: str, params: dict, *, retries: int = 8) -> dict:
    url = API + method + "?" + urllib.parse.urlencode(params)
    delay = 2.0
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "mayo-bot-backtest/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode())
            if payload.get("error"):
                err = ";".join(payload["error"])
                if "Too many requests" in err or "Unavailable" in err or "Busy" in err:
                    print(f"  rate-limited ({err}); backoff {delay:.0f}s", file=sys.stderr, flush=True)
                    time.sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
                raise RuntimeError(err)
            return payload["result"]
        except (OSError, ValueError) as exc:  # network / json
            if attempt == retries - 1:
                raise
            print(f"  retry {method} after {exc}", file=sys.stderr)
            time.sleep(delay)
            delay = min(delay * 2, 60)
    raise RuntimeError(f"{method} failed after {retries} retries")


def csv_path(symbol: str, tf: int, suffix: str = "") -> Path:
    base = symbol.replace("/", "")
    return DATA / f"kraken_{base}_{tf}m{suffix}.csv"


def load_cached(symbol: str, tf: int, suffix: str = "") -> pd.DataFrame:
    p = csv_path(symbol, tf, suffix)
    if not p.exists():
        return pd.DataFrame(columns=COLS)
    return pd.read_csv(p)


def merge_save(symbol: str, tf: int, df: pd.DataFrame, suffix: str = "") -> pd.DataFrame:
    old = load_cached(symbol, tf, suffix)
    frames = [f for f in (old, df) if len(f)]
    merged = pd.concat(frames) if frames else df
    merged = (
        merged.drop_duplicates(subset="time", keep="last")
        .sort_values("time")
        .reset_index(drop=True)
    )
    DATA.mkdir(parents=True, exist_ok=True)
    merged[COLS].to_csv(csv_path(symbol, tf, suffix), index=False)
    return merged


def fetch_ohlc(symbol: str, tf: int) -> pd.DataFrame:
    res = _get("OHLC", {"pair": PAIRS[symbol], "interval": tf})
    key = next(k for k in res if k != "last")
    rows = res[key]
    df = pd.DataFrame(
        [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[6])] for r in rows],
        columns=COLS,
    )
    # Drop the still-forming last candle.
    return df.iloc[:-1]


def cmd_ohlc(symbols: list[str], tfs: list[int]) -> None:
    for sym in symbols:
        for tf in tfs:
            df = fetch_ohlc(sym, tf)
            merged = merge_save(sym, tf, df)
            span = (merged["time"].iloc[-1] - merged["time"].iloc[0]) / 86400
            print(f"{sym} {tf}m: fetched {len(df)} cached {len(merged)} bars ({span:.1f} days)")
            time.sleep(1.1)


def resample(df15: pd.DataFrame, tf: int) -> pd.DataFrame:
    d = df15.copy()
    d["bucket"] = (d["time"] // (tf * 60)) * (tf * 60)
    g = d.groupby("bucket")
    out = pd.DataFrame({
        "time": g["time"].first().index.astype(int),
        "open": g["open"].first().values,
        "high": g["high"].max().values,
        "low": g["low"].min().values,
        "close": g["close"].last().values,
        "volume": g["volume"].sum().values,
    })
    # Only keep buckets that are complete (all sub-bars present is not
    # guaranteed on quiet pairs; require the bucket to be in the past).
    now_bucket = (int(time.time()) // (tf * 60)) * (tf * 60)
    return out[out["time"] < now_bucket].reset_index(drop=True)


def cmd_trades(symbols: list[str], days: int, pause: float) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    for sym in symbols:
        state_p = DATA / f".trades_state_{sym.replace('/', '')}.json"
        state = json.loads(state_p.read_text()) if state_p.exists() else {}
        start_ns = int((time.time() - days * 86400) * 1e9)
        cursor = int(state.get("cursor", start_ns))
        if cursor < start_ns and not state:
            cursor = start_ns
        buckets: dict[int, list[float]] = {}
        pages = 0
        stop_at = time.time() - 900  # stop once within the last 15m
        print(f"{sym}: paging trades from {pd.to_datetime(cursor, unit='ns')} UTC", flush=True)

        def flush(final: bool = False) -> None:
            if not buckets:
                return
            keys = sorted(buckets)
            # The newest bucket may still receive trades from the next page;
            # keep it in memory unless this is the final flush.
            done = keys if final else keys[:-1]
            rows = [[k, *buckets[k][:5]] for k in done]
            for k in done:
                del buckets[k]
            if rows:
                merge_save(sym, 15, pd.DataFrame(rows, columns=COLS), "_trades")

        while True:
            res = _get("Trades", {"pair": PAIRS[sym], "since": str(cursor), "count": 1000})
            key = next(k for k in res if k != "last")
            trades = res[key]
            new_cursor = int(res["last"])
            for t in trades:
                price, vol, ts = float(t[0]), float(t[1]), float(t[2])
                b = int(ts // 900) * 900
                cur = buckets.get(b)
                if cur is None:
                    buckets[b] = [price, price, price, price, vol]
                else:
                    cur[1] = max(cur[1], price)
                    cur[2] = min(cur[2], price)
                    cur[3] = price
                    cur[4] += vol
            pages += 1
            last_ts = float(trades[-1][2]) if trades else time.time()
            if pages % 20 == 0:
                flush()
                state_p.write_text(json.dumps({"cursor": new_cursor}))
                print(f"  {sym} page {pages} at {pd.to_datetime(last_ts, unit='s')} UTC", flush=True)
            if not trades or new_cursor == cursor or last_ts >= stop_at:
                cursor = new_cursor
                break
            cursor = new_cursor
            time.sleep(pause)
        flush(final=True)
        state_p.write_text(json.dumps({"cursor": cursor}))
        df15 = load_cached(sym, 15, "_trades")
        for tf in (60, 240):
            merge_save(sym, tf, resample(df15, tf), "_trades")
        span = (df15["time"].iloc[-1] - df15["time"].iloc[0]) / 86400
        print(f"{sym}: {len(df15)} 15m trade-built bars ({span:.1f} days), pages={pages}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["ohlc", "trades"])
    ap.add_argument("--symbols", nargs="*", default=list(PAIRS))
    ap.add_argument("--tfs", nargs="*", type=int, default=[15, 60, 240])
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--pause", type=float, default=1.0)
    a = ap.parse_args()
    if a.mode == "ohlc":
        cmd_ohlc(a.symbols, a.tfs)
    else:
        cmd_trades(a.symbols, a.days, a.pause)


if __name__ == "__main__":
    main()

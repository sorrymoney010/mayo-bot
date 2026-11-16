#!/usr/bin/env python3
"""Binance.US PUBLIC spot-market snapshot. Read-only; never submits trades.

Fetches current BTC/ETH and VET/HBAR/ALGO/XLM tape, plus a six-condition
Trend6 *diagnostic* from fully closed UTC daily candles. This is NOT a runner,
trade signal, position-sizing model, or an authority to restart paper trading.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://api.binance.us"
DAY_MS = 86_400_000
HOUR_MS = 3_600_000
SYMBOLS = ("BTCUSD", "ETHUSD", "VETUSD", "HBARUSD", "ALGOUSD", "XLMUSD")
TREND6_SYMBOLS = ("BTCUSD", "ETHUSD")


def utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def get_public(path: str, **params: object) -> object:
    """Only allowed public GET endpoints at the fixed Binance.US hostname."""
    allowed = {"/api/v3/time", "/api/v3/klines", "/api/v3/ticker/24hr"}
    if path not in allowed:
        raise ValueError("Non-public or unapproved endpoint")
    url = BASE + path + (("?" + urlencode(params)) if params else "")
    req = Request(url, headers={"User-Agent": "MAYO-read-only-tape/1.0"})
    with urlopen(req, timeout=12) as response:
        return json.loads(response.read(1_000_000).decode("utf-8"))


def closed_candles(rows: list, now_ms: int, period_ms: int) -> list[dict]:
    """Filter incomplete bars; demand sorted, nonduplicated, gap-free UTC bars."""
    result = []
    for row in rows:
        if len(row) < 7:
            raise ValueError("Malformed kline")
        open_ms, close_ms = int(row[0]), int(row[6])
        close = float(row[4])
        if not math.isfinite(close) or close <= 0 or close_ms < open_ms:
            raise ValueError("Invalid closed candle")
        if close_ms >= now_ms:  # Strictly before exchange's server clock
            continue
        result.append({"open_ms": open_ms, "close_ms": close_ms,
                       "close": close, "quote_volume": float(row[7]) if len(row) > 7 else None})
    if not result:
        return []
    for previous, current in zip(result, result[1:]):
        if current["open_ms"] - previous["open_ms"] != period_ms:
            raise ValueError("Gapped, duplicated or out-of-order candle history")
    return result


def trend6_diagnostic(rows: list, now_ms: int) -> dict:
    """Six yes/no conditions; no inferred exposure weights or order decision."""
    candles = closed_candles(rows, now_ms, DAY_MS)
    if len(candles) < 200:
        return {"status": "insufficient_history", "closed_days": len(candles)}
    if now_ms - candles[-1]["close_ms"] > 2 * DAY_MS:
        return {"status": "stale_daily_data", "closed_days": len(candles)}
    closes = [row["close"] for row in candles]
    last = closes[-1]
    checks = {
        "above_sma_50": last > sum(closes[-50:]) / 50,
        "above_sma_100": last > sum(closes[-100:]) / 100,
        "above_sma_200": last > sum(closes[-200:]) / 200,
        "positive_return_7d": last > closes[-8],
        "positive_return_14d": last > closes[-15],
        "positive_return_28d": last > closes[-29],
    }
    return {
        "status": "diagnostic_only",
        "closed_candle_utc": utc(candles[-1]["close_ms"]),
        "last_daily_close": last,
        "checks": checks,
        "conditions_on": sum(checks.values()),
        "conditions_total": 6,
        "position_size": "NOT_CALCULATED: original Trend6 mapping unverified",
    }


def snapshot() -> dict:
    server = get_public("/api/v3/time")
    now_ms = int(server["serverTime"])
    output = {
        "venue": "binance_us", "market": "spot", "quote": "USD",
        "server_time_utc": utc(now_ms),
        "server_time_ms": now_ms,
        "mode": "PUBLIC_DATA_ONLY_NO_ORDER_PATH",
        "note": "24h rolling tickers are observed now; Trend6 uses last FULL UTC daily candle only.",
        "symbols": {},
    }
    for symbol in SYMBOLS:
        record = {}
        try:
            info = get_public("/api/v3/ticker/24hr", symbol=symbol)
            last = float(info["lastPrice"])
            if not math.isfinite(last) or last <= 0:
                raise ValueError("Invalid last price")
            record["ticker"] = {
                "observed_at_utc": utc(now_ms),
                "last_price": last, "change_24h_pct": float(info["priceChangePercent"]),
                "quote_volume_24h": float(info.get("quoteVolume", 0)),
                "source": "Binance.US /api/v3/ticker/24hr",
            }
            hourly = get_public("/api/v3/klines", symbol=symbol, interval="1h", limit=4)
            completed = closed_candles(hourly, now_ms, HOUR_MS)
            record["last_completed_hour"] = (
                {"close_utc": utc(completed[-1]["close_ms"]),
                 "close_price": completed[-1]["close"],
                 "fresh": now_ms - completed[-1]["close_ms"] <= 2 * HOUR_MS}
                if completed else {"status": "no_closed_hour"}
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            record["tape_error"] = type(exc).__name__ + ": " + str(exc)[:120]
        if symbol in TREND6_SYMBOLS:
            try:
                daily = get_public("/api/v3/klines", symbol=symbol,
                                   interval="1d", limit=230)
                record["trend6"] = trend6_diagnostic(daily, now_ms)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                record["trend6"] = {"status": "unavailable",
                                    "error": type(exc).__name__ + ": " + str(exc)[:120]}
        output["symbols"][symbol] = record
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", help="Optional standalone JSON file; NEVER a trading ledger")
    args = parser.parse_args()
    result = snapshot()
    payload = json.dumps(result, indent=2, allow_nan=False)
    if args.out:
        from pathlib import Path
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Read-only evidence audit for a four-coin passive limit-entry candidate.

This script NEVER connects to an exchange, submits orders, evaluates exits, or
writes to the running MAYO paper ledger. A traded-through print is a fill
*proxy*, not a confirmed exchange execution (queue position is unknown).
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

COINS = ("VET", "HBAR", "ALGO", "XLM")
VENUES = ("binance_us", "binance_com")
QUOTES = ("USD", "USDT")
DISCOUNT = Decimal("0.995")


def _positive_decimal(row: dict, key: str) -> Decimal:
    try:
        value = Decimal(row[key])
    except (KeyError, InvalidOperation) as exc:
        raise ValueError(f"Invalid {key} in {row!r}") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{key} must be positive and finite")
    return value


def _integer(row: dict, key: str) -> int:
    try:
        return int(row[key])
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid {key} in {row!r}") from exc


def _validate(row: dict, venue: str, quote: str) -> str:
    if row.get("venue") != venue:
        raise ValueError("Mixed/mismatched data venues are not allowed")
    symbol = row.get("symbol", "")
    if symbol not in {base + quote for base in COINS}:
        raise ValueError(f"Symbol {symbol!r} is not in the frozen {quote} universe")
    return symbol


def load_csv(path: str | Path, required: set[str]) -> list[dict]:
    with Path(path).open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(f"{path}: expected columns {sorted(required)}")
        return list(reader)


def evaluate(
    signals: list[dict],
    prints: list[dict],
    coverage: list[dict],
    *,
    venue: str,
    quote: str,
) -> list[dict]:
    """Audit one frozen venue/quote cohort; no inferred TTL or strategy exits.

    Signals: signal_id,venue,symbol,close_ms,close_price,expires_ms
    Prints: venue,symbol,trade_ms,price (completed public trade prints)
    Coverage: venue,symbol,start_ms,end_ms,gap_count
    A coverage row must attest to an uninterrupted feed spanning the entire
    signal window. False assurances in a manifest cannot be detected here.
    """
    if venue not in VENUES or quote not in QUOTES:
        raise ValueError("Select a single supported venue and quote currency")

    feeds: dict[str, list[tuple[int, Decimal]]] = defaultdict(list)
    for row in prints:
        symbol = _validate(row, venue, quote)
        feeds[symbol].append((_integer(row, "trade_ms"), _positive_decimal(row, "price")))
    for series in feeds.values():
        series.sort()

    spans = {}
    for row in coverage:
        symbol = _validate(row, venue, quote)
        if symbol in spans:
            raise ValueError(f"Duplicate coverage records for {symbol}")
        start = _integer(row, "start_ms")
        end = _integer(row, "end_ms")
        gaps = _integer(row, "gap_count")
        if start >= end or gaps < 0:
            raise ValueError("Invalid coverage interval or gap count")
        spans[symbol] = (start, end, gaps)

    output = []
    seen = set()
    for row in signals:
        symbol = _validate(row, venue, quote)
        signal_id = row.get("signal_id", "")
        if not signal_id or signal_id in seen:
            raise ValueError("Signal IDs must be nonempty and unique")
        seen.add(signal_id)
        closed = _integer(row, "close_ms")
        expires = _integer(row, "expires_ms")
        if closed < 0 or expires <= closed:
            raise ValueError("Signal must expire strictly after its closed candle")
        limit = _positive_decimal(row, "close_price") * DISCOUNT
        status, observed_ms, observed_price = "coverage_unknown", None, None
        span = spans.get(symbol)
        if span and span[0] <= closed and span[1] >= expires and span[2] == 0:
            status = "no_touch"
            first_touch = None
            for when, price in feeds[symbol]:
                if when <= closed or when >= expires:
                    continue  # Never same-candle, never at/after expiry
                if price < limit:
                    status, observed_ms, observed_price = "traded_through", when, str(price)
                    break  # Stronger fill proxy; still not a confirmed fill
                if price == limit and first_touch is None:
                    first_touch = (when, str(price))
            if status == "no_touch" and first_touch:
                status = "touch_only"
                observed_ms, observed_price = first_touch
        output.append({
            "signal_id": signal_id,
            "venue": venue,
            "symbol": symbol,
            "close_ms": closed,
            "expires_ms": expires,
            "limit_price": str(limit),
            "status": status,
            "observed_trade_ms": observed_ms,
            "observed_trade_price": observed_price,
            "confirmed_fill": False,  # Public prints cannot prove queue priority
        })
    return output


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--signals", required=True)
    p.add_argument("--prints", required=True)
    p.add_argument("--coverage", required=True)
    p.add_argument("--venue", required=True, choices=VENUES)
    p.add_argument("--quote", required=True, choices=QUOTES)
    args = p.parse_args()
    rows = evaluate(
        load_csv(args.signals, {"signal_id", "venue", "symbol", "close_ms",
                                "close_price", "expires_ms"}),
        load_csv(args.prints, {"venue", "symbol", "trade_ms", "price"}),
        load_csv(args.coverage, {"venue", "symbol", "start_ms", "end_ms", "gap_count"}),
        venue=args.venue, quote=args.quote,
    )
    print(json.dumps({"read_only": True, "confirmed_fills": 0, "signals": rows}, indent=2))


if __name__ == "__main__":
    main()

"""Shared-book add-ons for the futures paper ledger.

Spot sleeves keep their own cash + cost-basis equity. Futures margin that has
been reserved out of that cash is added back here, plus unrealized short P&L,
so the book is not understated. Open futures also count toward the global
position cap and the exposure cap. No network, no orders.
"""
from __future__ import annotations

import json
from pathlib import Path

FUTURES_LEDGER = Path("logs/paper_futures.json")


def load_futures_ledger(path: Path | str | None = None) -> dict:
    p = Path(path) if path is not None else FUTURES_LEDGER
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"positions": {}, "margin_locked": 0.0}
    if not isinstance(data, dict):
        return {"positions": {}, "margin_locked": 0.0}
    pos = data.get("positions") or {}
    if not isinstance(pos, dict):
        pos = {}
    return {"positions": pos, "margin_locked": float(data.get("margin_locked") or 0.0)}


def futures_position_count(path: Path | str | None = None) -> int:
    book = load_futures_ledger(path)
    return sum(1 for p in book["positions"].values()
               if isinstance(p, dict) and abs(float(p.get("qty") or 0.0)) > 1e-12)


def futures_exposure_usd(path: Path | str | None = None) -> float:
    """Absolute notional of open paper perps (counts toward the exposure cap)."""
    book = load_futures_ledger(path)
    total = 0.0
    for p in book["positions"].values():
        if not isinstance(p, dict):
            continue
        try:
            total += abs(float(p.get("notional") or 0.0))
        except (TypeError, ValueError):
            return float("inf")
    return total


def futures_equity_addon(path: Path | str | None = None) -> float:
    """Margin still locked plus unrealized P&L already stored on the ledger.

    Cash was reduced by the margin when the position opened, so equity readers
    that only see spot cash must add this back.
    """
    book = load_futures_ledger(path)
    extra = 0.0
    for p in book["positions"].values():
        if not isinstance(p, dict):
            continue
        try:
            # Funding is paid into spot cash when it accrues, so it is already
            # inside the cash the caller adds. Counting funding_usd again would
            # double-count it.
            extra += float(p.get("margin") or 0.0) + float(p.get("unrealized") or 0.0)
        except (TypeError, ValueError):
            continue
    return extra

"""Paper model of Kraken spot-margin borrow cost.

Kraken charges, on top of the normal spot trade fee, an opening fee when the
margin is extended and a rollover every 4 hours. Rates are dynamic and locked
at order time (https://support.kraken.com/articles/206161568-what-are-the-fees-opening-and-rollover-for-trading-using-margin-).
The published bands used here, conservative top of range:

* BTC: opening 0.02%, rollover 0.02% per 4 hours
* ETH, SOL and other listed majors: opening 0.04%, rollover 0.04% per 4 hours

This module does not submit margin orders and does not call private endpoints
(borrow availability and margin level are private). Short *spot* is therefore
not a paper sleeve. Shorting is the perpetual sleeve in ``futures_sleeve``:
the public mark, funding rate and open interest are enough to simulate a
short without borrowing the coin. See docs/PAPER_PRO.md.

Live spot-margin orders go through ``venue_locks.submit_margin_order``, which
stays off unless ``ALLOW_MARGIN_LIVE_ORDERS`` and every existing lock are open.
"""
from __future__ import annotations

# Top of the published band, as a fraction (not bps).
MARGIN_OPEN_FEE = {
    "BTC": 0.0002,
    "ETH": 0.0004,
    "SOL": 0.0004,
}
MARGIN_ROLLOVER_4H = {
    "BTC": 0.0002,
    "ETH": 0.0004,
    "SOL": 0.0004,
}
DEFAULT_OPEN_FEE = 0.0004
DEFAULT_ROLLOVER_4H = 0.0004
ROLLOVER_HOURS = 4.0


def _asset(symbol: str) -> str:
    base = symbol.upper().split("/")[0]
    if base in ("XBT", "XXBT"):
        return "BTC"
    return base


def opening_fee_rate(symbol: str) -> float:
    return MARGIN_OPEN_FEE.get(_asset(symbol), DEFAULT_OPEN_FEE)


def rollover_rate_per_4h(symbol: str) -> float:
    return MARGIN_ROLLOVER_4H.get(_asset(symbol), DEFAULT_ROLLOVER_4H)


def margin_borrow_cost(symbol: str, borrowed_notional: float, *, hours_open: float) -> dict:
    """USD cost of borrowing ``borrowed_notional`` for ``hours_open``.

    Opening fee is charged once. Rollover accrues once per started 4-hour
    block (Kraken's rollover clock), using the top of the published band.
    """
    notion = abs(float(borrowed_notional))
    hours = max(float(hours_open), 0.0)
    blocks = int(hours // ROLLOVER_HOURS)
    # A position open for a partial block has not yet rolled. Zero hours => 0 rollover.
    open_rate = opening_fee_rate(symbol)
    roll_rate = rollover_rate_per_4h(symbol)
    opening = notion * open_rate
    rollover = notion * roll_rate * blocks
    return {
        "symbol": symbol.upper(),
        "borrowed_notional": notion,
        "hours_open": hours,
        "rollover_blocks": blocks,
        "opening_fee_rate": open_rate,
        "rollover_rate_per_4h": roll_rate,
        "opening_fee_usd": opening,
        "rollover_fee_usd": rollover,
        "total_usd": opening + rollover,
        "note": ("Paper model uses the top of Kraken's published margin band. "
                 "Live rates are dynamic. Short spot is not traded; perpetuals cover shorting."),
    }

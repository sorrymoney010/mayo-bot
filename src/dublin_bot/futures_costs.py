"""Kraken Futures paper costs and isolated-margin liquidation.

Published base-tier perpetuals schedule (Kraken "Fees for Derivatives trading",
https://support.kraken.com/articles/360048917612-fee-schedule): maker 0.0200%,
taker 0.0500% of notional. Funding is not an exchange fee. Kraken realises the
perpetual funding rate every hour: a positive rate means longs pay shorts.

Spot fees stay on the existing 40/25/10 bps model. This module is futures only.
"""
from __future__ import annotations

# Base tier, $0 thirty-day volume. bps.
FUTURES_MAKER_FEE_BPS = 2.0
FUTURES_TAKER_FEE_BPS = 5.0

# Hard ceiling for paper leverage. Not configurable upward.
MAX_PAPER_LEVERAGE = 2.0

# Small-size maintenance margin used when the public instrument payload does
# not carry one. Isolated liquidation uses this; it is not exchange leverage.
DEFAULT_MAINTENANCE_MARGIN = 0.01

SPOT_TO_PERP = {
    "BTC/USD": "PF_XBTUSD",
    "ETH/USD": "PF_ETHUSD",
    "SOL/USD": "PF_SOLUSD",
}
PERP_TO_SPOT = {v: k for k, v in SPOT_TO_PERP.items()}


def clamp_leverage(value: float) -> float:
    """Paper leverage in [1, 2]. Values above the cap are cut, not rejected here."""
    try:
        lev = float(value)
    except (TypeError, ValueError):
        lev = 1.0
    if lev < 1.0:
        return 1.0
    if lev > MAX_PAPER_LEVERAGE:
        return MAX_PAPER_LEVERAGE
    return lev


def fee_usd(notional: float, *, maker: bool = False) -> float:
    bps = FUTURES_MAKER_FEE_BPS if maker else FUTURES_TAKER_FEE_BPS
    return abs(float(notional)) * bps / 1e4


def liquidation_price(entry: float, *, side: str, leverage: float,
                      maintenance: float = DEFAULT_MAINTENANCE_MARGIN) -> float:
    """Isolated liquidation price.

    Initial margin fraction is 1/leverage. The position liquidates when the
    loss of margin reaches initial minus maintenance:

    * long:  entry * (1 - 1/leverage + maintenance)
    * short: entry * (1 + 1/leverage - maintenance)
    """
    lev = clamp_leverage(leverage)
    mm = min(max(float(maintenance), 0.0), 1.0 / lev - 1e-9)
    if side == "short":
        return float(entry) * (1.0 + 1.0 / lev - mm)
    return float(entry) * (1.0 - 1.0 / lev + mm)


def relative_funding_rate(absolute_rate: float | None, mark: float | None) -> float | None:
    """Convert Kraken's ticker ``fundingRate`` into a fraction of notional.

    The public ticker publishes ``fundingRate`` in quote currency per base unit
    per hour (for PF_XBTUSD, USD per BTC). Historical rows also publish
    ``relativeFundingRate``, which is that absolute rate divided by the mark.
    Cash on a USD notional uses the relative rate. Returns None when the mark
    is missing so the caller skips the accrual instead of treating the absolute
    rate as a fraction of notional.
    """
    if absolute_rate is None or mark is None:
        return None
    try:
        mark_f = float(mark)
        rate_f = float(absolute_rate)
    except (TypeError, ValueError):
        return None
    if mark_f <= 0:
        return None
    return rate_f / mark_f


def funding_cashflow(*, side: str, notional: float, hourly_rate: float, hours: float = 1.0) -> float:
    """Cash credited to the paper account for ``hours`` of funding.

    ``hourly_rate`` is the relative rate (fraction of notional per hour).
    Positive means longs pay shorts, so a short receives ``rate * notional``.
    """
    sign = -1.0 if side == "short" else 1.0
    # Long pays when rate > 0, so the account delta is -sign * rate * notional.
    return -sign * float(hourly_rate) * abs(float(notional)) * float(hours)


def short_pnl(entry: float, exit_px: float, notional: float) -> float:
    """Linear perp short: profit when price falls. ``notional`` is entry notional."""
    if entry <= 0:
        return 0.0
    return (float(entry) - float(exit_px)) / float(entry) * abs(float(notional))

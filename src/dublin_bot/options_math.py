"""Small, dependency-free option maths: Black-Scholes, implied vol, OCC symbols.

tastytrade's REST quotes carry prices but no Greeks (Greeks are DXLink-stream
only), so the sleeve backs delta out of each put's mid price: solve implied
volatility by bisection, then take the Black-Scholes delta.  For 30-60 DTE
index-ETF puts this matches broker Greeks to within ~1 delta point, which is
far finer than the ±8-point tolerance the strike selector uses.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime

_SQRT2 = math.sqrt(2.0)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


def _d1(s: float, k: float, t: float, r: float, q: float, vol: float) -> float:
    return (math.log(s / k) + (r - q + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))


def bs_put(s: float, k: float, t: float, vol: float, r: float = 0.04, q: float = 0.0) -> float:
    if t <= 0 or vol <= 0:
        return max(k - s, 0.0)
    d1 = _d1(s, k, t, r, q, vol)
    d2 = d1 - vol * math.sqrt(t)
    return k * math.exp(-r * t) * norm_cdf(-d2) - s * math.exp(-q * t) * norm_cdf(-d1)


def bs_put_delta(s: float, k: float, t: float, vol: float, r: float = 0.04, q: float = 0.0) -> float:
    """Put delta (negative, in [-1, 0])."""
    if t <= 0 or vol <= 0:
        return -1.0 if s < k else 0.0
    return -math.exp(-q * t) * norm_cdf(-_d1(s, k, t, r, q, vol))


def implied_vol_put(price: float, s: float, k: float, t: float, r: float = 0.04,
                    q: float = 0.0, lo: float = 1e-4, hi: float = 5.0) -> float | None:
    """Bisection IV for a put. None when the price is outside no-arbitrage bounds."""
    if price <= 0 or t <= 0:
        return None
    intrinsic = max(k * math.exp(-r * t) - s * math.exp(-q * t), 0.0)
    if price < intrinsic - 1e-9 or price > k:
        return None
    f_lo, f_hi = bs_put(s, k, t, lo, r, q) - price, bs_put(s, k, t, hi, r, q) - price
    if f_lo > 0 or f_hi < 0:
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if bs_put(s, k, t, mid, r, q) > price:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-6:
            break
    return 0.5 * (lo + hi)


# ── OCC symbology ──────────────────────────────────────────
_OCC_RE = re.compile(r"^(?P<root>.{1,6}?)\s*(?P<ymd>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")


@dataclass(frozen=True)
class OccSymbol:
    root: str
    expiration: date
    right: str  # "P" or "C"
    strike: float

    def __str__(self) -> str:
        return occ_symbol(self.root, self.expiration, self.right, self.strike)


def occ_symbol(root: str, expiration: date, right: str, strike: float) -> str:
    """'SPY', 2026-12-31, 'P', 600 -> 'SPY   261231P00600000'."""
    if right not in ("P", "C"):
        raise ValueError("right must be 'P' or 'C'")
    milli = int(round(strike * 1000))
    if milli <= 0 or milli >= 10 ** 8:
        raise ValueError(f"strike out of OCC range: {strike}")
    return f"{root.upper():<6}{expiration:%y%m%d}{right}{milli:08d}"


def parse_occ(symbol: str) -> OccSymbol:
    m = _OCC_RE.match(symbol.strip())
    if not m:
        raise ValueError(f"not an OCC option symbol: {symbol!r}")
    exp = datetime.strptime(m["ymd"], "%y%m%d").date()
    return OccSymbol(root=m["root"].strip(), expiration=exp, right=m["cp"],
                     strike=int(m["strike"]) / 1000.0)

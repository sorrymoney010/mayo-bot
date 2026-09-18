from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MarketQuality:
    bid: float
    ask: float
    recent_dollar_volume: float

    @property
    def spread_bps(self) -> float:
        mid = (self.bid + self.ask) / 2
        return ((self.ask - self.bid) / mid) * 10_000 if mid > 0 else float("inf")


class MarketGuard:
    def __init__(self, max_spread_bps: float, min_dollar_volume: float) -> None:
        self.max_spread_bps = max_spread_bps
        self.min_dollar_volume = min_dollar_volume

    def approve(self, quality: MarketQuality) -> tuple[bool, str]:
        if quality.bid <= 0 or quality.ask <= quality.bid:
            return False, "Invalid or crossed market quote"
        if quality.spread_bps > self.max_spread_bps:
            return False, f"Spread too wide: {quality.spread_bps:.1f} bps"
        if quality.recent_dollar_volume < self.min_dollar_volume:
            return False, "Recent dollar volume is below minimum"
        return True, "Liquidity and spread checks passed"


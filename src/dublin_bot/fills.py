"""Realistic paper fill model for dry-run and simulated trading.

The goal is not to perfectly predict live execution.  It is to make paper
results *directionally credible* so that learning, journaling, and
risk-adjusted metrics are not trivially optimistic.

Defaults are conservative for a low-equity starter account:
- taker fee: 26 bps
- slippage: 10 bps
- spread assumption: mid +/- half the quoted spread when available, else a
  minimal baseline spread.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Settings


@dataclass(frozen=True)
class FillSpec:
    side: str
    price: float
    volume: float
    notional: float
    fee: float
    slippage: float
    spread_bps: float
    model_note: str


class FillModel:
    """Computes simulated fills for paper trades."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.taker_fee_bps = float(getattr(settings, "paper_taker_fee_bps", 80.0))
        self.maker_fee_bps = float(
            getattr(settings, "paper_maker_fee_bps", self.taker_fee_bps)
        )
        self.slippage_bps = float(getattr(settings, "paper_slippage_bps", 10.0))
        self.min_spread_bps = float(getattr(settings, "paper_min_spread_bps", 5.0))

    def buy(self, *, price: float, volume: float, bid: float | None = None, ask: float | None = None, maker: bool = False) -> FillSpec:
        return self._fill("buy", price=price, volume=volume, bid=bid, ask=ask, maker=maker)

    def sell(self, *, price: float, volume: float, bid: float | None = None, ask: float | None = None, maker: bool = False) -> FillSpec:
        return self._fill("sell", price=price, volume=volume, bid=bid, ask=ask, maker=maker)

    def _fill(self, side: str, *, price: float, volume: float, bid: float | None, ask: float | None, maker: bool = False) -> FillSpec:
        if price <= 0 or volume <= 0:
            raise ValueError("price and volume must be positive for fill simulation")

        mid = price
        if bid is not None and ask is not None and ask > bid:
            mid = (bid + ask) / 2.0
            spread_bps = ((ask - bid) / mid) * 10_000.0
        else:
            spread_bps = self.min_spread_bps

        half_spread = mid * (spread_bps / 2.0) / 10_000.0
        # A passive maker order crosses no spread and suffers no slippage; it
        # earns the maker rebate-tier fee instead of paying taker.
        if maker:
            slippage = 0.0
            fee_bps = self.maker_fee_bps
            if side == "buy":
                fill_price = mid - half_spread
            else:
                fill_price = mid + half_spread
        else:
            slippage = mid * (self.slippage_bps / 10_000.0)
            fee_bps = self.taker_fee_bps
            if side == "buy":
                fill_price = mid + half_spread + slippage
            else:
                fill_price = mid - half_spread - slippage

        fee = mid * volume * (fee_bps / 10_000.0)
        fill_price = max(fill_price, 1e-12)
        notional = fill_price * volume
        model_note = (
            f"mid={mid:.2f} spread={spread_bps:.2f}bps "
            f"slip={slippage:.6f} fee={fee_bps:.2f}bps "
            f"{'maker' if maker else 'taker'}"
        )
        return FillSpec(
            side=side,
            price=round(fill_price, 8),
            volume=volume,
            notional=round(notional, 8),
            fee=round(fee, 8),
            slippage=round(slippage, 8),
            spread_bps=round(spread_bps, 4),
            model_note=model_note,
        )

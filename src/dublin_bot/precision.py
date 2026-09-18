"""Order precision and minimum-size enforcement.

Kraken rejects orders whose volume has more decimals than ``lot_decimals`` or
whose size is below the pair's ``ordermin`` / ``costmin``.  A rejected order in a
live system is not merely noise: the strategy believes it is in a position when
it is not, so reconciliation drifts.

All arithmetic uses ``Decimal`` with explicit ``ROUND_DOWN``.  Rounding *down* is
mandatory — rounding up can exceed the risk-sized notional and breach the
position cap, whereas rounding down only ever trades slightly less than planned.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, InvalidOperation

from .errors import PrecisionError


@dataclass(frozen=True)
class PairPrecision:
    """Precision and minimum constraints for one Kraken pair."""

    pair: str
    lot_decimals: int
    pair_decimals: int
    order_min: Decimal
    cost_min: Decimal = Decimal("0")

    @classmethod
    def from_kraken(cls, pair: str, info: dict) -> "PairPrecision":
        return cls(
            pair=pair,
            lot_decimals=int(info.get("lot_decimals", 8)),
            pair_decimals=int(info.get("pair_decimals", 8)),
            order_min=_to_decimal(info.get("ordermin", "0")),
            cost_min=_to_decimal(info.get("costmin", "0")),
        )


def _to_decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PrecisionError(f"cannot interpret {value!r} as a decimal") from exc


def _quantize_down(value: Decimal, decimals: int) -> Decimal:
    if decimals < 0:
        raise PrecisionError(f"invalid decimals: {decimals}")
    exponent = Decimal(1).scaleb(-decimals)
    return value.quantize(exponent, rounding=ROUND_DOWN)


def round_volume(volume: float | Decimal, precision: PairPrecision) -> Decimal:
    """Truncate a volume to the pair's lot precision (never rounds up)."""
    return _quantize_down(_to_decimal(volume), precision.lot_decimals)


def round_price(price: float | Decimal, precision: PairPrecision) -> Decimal:
    """Truncate a price to the pair's quote precision."""
    return _quantize_down(_to_decimal(price), precision.pair_decimals)


@dataclass(frozen=True)
class SizedOrder:
    """A fully validated, exchange-ready order size."""

    pair: str
    volume: Decimal
    price: Decimal
    notional: Decimal

    @property
    def volume_str(self) -> str:
        """Kraken wants a plain decimal string, never scientific notation."""
        return format(self.volume, "f")

    @property
    def price_str(self) -> str:
        return format(self.price, "f")


def size_order(
    notional_usd: float | Decimal,
    price: float | Decimal,
    precision: PairPrecision,
    *,
    min_notional_usd: float | Decimal = 0,
) -> SizedOrder:
    """Convert a USD notional into a validated exchange volume.

    Raises ``PrecisionError`` when the resulting order would be rejected by the
    exchange or would fall below the configured minimum notional.  Failing here
    is intentional: it keeps unfillable orders out of the order path entirely.
    """
    notional = _to_decimal(notional_usd)
    px = _to_decimal(price)
    if px <= 0:
        raise PrecisionError(f"invalid price for {precision.pair}: {px}")
    if notional <= 0:
        raise PrecisionError(f"invalid notional for {precision.pair}: {notional}")

    rounded_price = round_price(px, precision)
    if rounded_price <= 0:
        raise PrecisionError(
            f"price {px} rounds to zero at {precision.pair_decimals} decimals"
        )

    volume = round_volume(notional / px, precision)
    if volume <= 0:
        raise PrecisionError(
            f"notional {notional} at price {px} rounds to zero volume "
            f"({precision.lot_decimals} lot decimals) for {precision.pair}"
        )
    if precision.order_min > 0 and volume < precision.order_min:
        raise PrecisionError(
            f"volume {volume} below Kraken minimum {precision.order_min} "
            f"for {precision.pair}"
        )

    actual_notional = volume * px
    if precision.cost_min > 0 and actual_notional < precision.cost_min:
        raise PrecisionError(
            f"order cost {actual_notional} below Kraken minimum cost "
            f"{precision.cost_min} for {precision.pair}"
        )
    floor = _to_decimal(min_notional_usd)
    if floor > 0 and actual_notional < floor:
        raise PrecisionError(
            f"order cost {actual_notional} below configured minimum {floor}"
        )

    return SizedOrder(
        pair=precision.pair,
        volume=volume,
        price=rounded_price,
        notional=actual_notional,
    )

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProtectiveStop:
    symbol: str
    stop_price: float
    quantity: float


class StopMonitor:
    """Pure stop evaluator designed to run outside the entry loop."""

    @staticmethod
    def should_exit(last_price: float, stop: ProtectiveStop) -> bool:
        if last_price <= 0 or stop.stop_price <= 0 or stop.quantity <= 0:
            raise ValueError("invalid stop monitoring values")
        return last_price <= stop.stop_price


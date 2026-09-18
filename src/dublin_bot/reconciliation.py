from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    quantity: float
    market_value: float
    average_entry: float


@dataclass(frozen=True)
class OrderSnapshot:
    order_id: str
    symbol: str
    side: str
    status: str


@dataclass(frozen=True)
class ReconciliationReport:
    positions: tuple[PositionSnapshot, ...]
    open_orders: tuple[OrderSnapshot, ...]
    orphaned_symbols: tuple[str, ...]
    checked_at: str


def reconcile(
    positions: list[PositionSnapshot],
    open_orders: list[OrderSnapshot],
    managed_symbols: set[str],
) -> ReconciliationReport:
    active = {p.symbol.replace("/", "") for p in positions}
    active.update(o.symbol.replace("/", "") for o in open_orders)
    managed = {s.replace("/", "") for s in managed_symbols}
    return ReconciliationReport(
        positions=tuple(positions),
        open_orders=tuple(open_orders),
        orphaned_symbols=tuple(sorted(active - managed)),
        checked_at=datetime.now(timezone.utc).isoformat(),
    )


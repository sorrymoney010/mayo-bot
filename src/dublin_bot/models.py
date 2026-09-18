from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class Action(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    WAIT = "WAIT"
    HALT = "HALT"


@dataclass(frozen=True)
class Signal:
    action: Action
    score: int
    reason: str
    price: float
    atr: float | None = None
    stop_price: float | None = None


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str
    notional_usd: float = 0.0
    planned_loss_usd: float = 0.0


@dataclass(frozen=True)
class DecisionRecord:
    symbol: str
    signal: Signal
    risk: RiskDecision
    dry_run: bool
    order_id: str | None = None
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["signal"]["action"] = self.signal.action.value
        value["timestamp"] = self.timestamp or datetime.now(timezone.utc).isoformat()
        return value


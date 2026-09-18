"""DCA accumulator sleeve — fixed-USD accumulation under the existing safety gates.

This is a *complementary* positioning mode, not a signal. Where the main
mean-reversion strategy trades dips, the DCA sleeve mechanically accumulates
``dca_symbol`` (default PUMP/USD) on a fixed interval, up to a position cap,
subject to the SAME risk breakers as every other order (daily-loss, drawdown,
exposure cap, sentiment filter, idempotency, precision).

Design notes
------------
* DCA does NOT bypass the engine's execution path. It emits a ``Signal(BUY)``
  that flows through Gate 5.5 (sentiment) and Gate 6 (``RiskManager``) and is
  placed via the same ``_execute`` call as mean-reversion — so idempotency,
  precision, and gateway safety all still apply.
* Because ``RiskManager.evaluate`` requires a valid stop distance, the DCA
  signal carries a synthetic stop (price minus ``dca_stop_buffer``) purely so
  the gate can size/approve it. The stop is not a real exit rule — DCA exits
  are handled by the main strategy or manual control.
* Interval and cap enforcement live HERE (``DCAAccumulator``), independent of
  the engine's per-trade cooldown, so DCA can accumulate on its own cadence
  without being blocked by (or blocking) mean-reversion entries.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .config import Settings
from .models import Action, Signal


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class DCAState:
    """Persisted DCA sleeve state (kept tiny; lives in the session store)."""

    last_buy_at: datetime | None = None
    buys_today: int = 0
    total_buys: int = 0
    # Date string (UTC) used to reset the per-day buy counter.
    day: str = field(default_factory=lambda: _now().date().isoformat())

    def roll_day(self) -> None:
        today = _now().date().isoformat()
        if today != self.day:
            self.day = today
            self.buys_today = 0

    def to_dict(self) -> dict:
        return {
            "last_buy_at": self.last_buy_at.isoformat() if self.last_buy_at else None,
            "buys_today": self.buys_today,
            "total_buys": self.total_buys,
            "day": self.day,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DCAState":
        last = data.get("last_buy_at")
        return cls(
            last_buy_at=datetime.fromisoformat(last) if last else None,
            buys_today=int(data.get("buys_today", 0)),
            total_buys=int(data.get("total_buys", 0)),
            day=data.get("day", _now().date().isoformat()),
        )


class DCAAccumulator:
    """Decides whether a DCA BUY is due this cycle.

    Pure logic — no network, no side effects. The engine calls
    :meth:`maybe_signal` and, if it returns a BUY, routes it through the normal
    risk/execution gates.
    """

    def __init__(self, settings: Settings, now: Callable[[], datetime] = _now) -> None:
        self.s = settings
        self._now = now
        self._state_path: Path | None = None

    # ── gating ──────────────────────────────────────────────────
    def _interval_ok(self, state: DCAState) -> tuple[bool, str]:
        if state.last_buy_at is None:
            return True, "no prior DCA buy"
        ready_at = state.last_buy_at + timedelta(minutes=self.s.dca_interval_minutes)
        if self._now() >= ready_at:
            return True, f"interval elapsed ({self.s.dca_interval_minutes}m)"
        return False, "DCA interval not elapsed"

    def _cap_ok(self, state: DCAState) -> tuple[bool, str]:
        if self.s.dca_max_buys_per_day > 0 and state.buys_today >= self.s.dca_max_buys_per_day:
            return False, f"daily DCA cap reached ({state.buys_today}/{self.s.dca_max_buys_per_day})"
        if self.s.dca_max_total_buys > 0 and state.total_buys >= self.s.dca_max_total_buys:
            return False, f"total DCA cap reached ({state.total_buys}/{self.s.dca_max_total_buys})"
        return True, "under caps"

    def maybe_signal(
        self,
        state: DCAState,
        price: float,
        in_position: bool,
        mr_action_is_buy: bool,
    ) -> Signal | None:
        """Return a DCA BUY signal if one is due, else ``None``.

        ``mr_action_is_buy`` lets the engine suppress DCA when the main strategy
        is already entering, so we never double-buy the same bar.
        """
        if not self.s.dca_enabled:
            return None
        if in_position:
            return None  # don't stack DCA on top of an open MR position
        if mr_action_is_buy:
            return None  # main strategy is handling this bar
        state.roll_day()
        ok_interval, why_interval = self._interval_ok(state)
        ok_cap, why_cap = self._cap_ok(state)
        if not (ok_interval and ok_cap):
            return None
        if price <= 0:
            return None
        # Synthetic stop so RiskManager.evaluate's stop-distance check passes;
        # this is NOT a real exit rule.
        stop = price * (1.0 - self.s.dca_stop_buffer)
        return Signal(
            Action.BUY,
            60,
            f"DCA accumulator: {self.s.dca_fixed_usd:.2f} USD on {self.s.dca_symbol} "
            f"({why_interval}; {why_cap})",
            price=price,
            stop_price=stop,
        )

    def record_buy(self, state: DCAState) -> None:
        """Update persisted counters after a DCA BUY is executed."""
        state.roll_day()
        state.last_buy_at = self._now()
        state.buys_today += 1
        state.total_buys += 1
        self.save(state)

    # ── persistence ──────────────────────────────────────────
    def load(self, path) -> DCAState:
        """Load persisted DCA state; returns a fresh state if none exists."""
        p = Path(path)
        if p.exists():
            try:
                return DCAState.from_dict(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        return DCAState()

    def save(self, state: DCAState, path=None) -> None:
        p = Path(path) if path else getattr(self, "_state_path", None)
        if p is None:
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
        except OSError:
            pass

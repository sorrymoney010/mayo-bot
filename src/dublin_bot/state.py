from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path

from .risk import SessionState


class StateStore:
    """Durable daily risk state stored locally and excluded from Git."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self, equity: float) -> SessionState:
        today = date.today().isoformat()
        if not self.path.exists():
            return SessionState(equity, equity, equity)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return SessionState(equity, equity, equity)
        if data.get("session_date") != today:
            return SessionState(equity, equity, equity)
        last_order = data.get("last_order_at")
        persisted_start = float(data.get("start_equity", equity))
        # Reset a stale session. start_equity (and the derived peak / day counters)
        # are only meaningful for the current funded balance; a persisted value
        # far from the live equity (e.g. an old $25 default, a prior $1000
        # balance, or a bot restart after a deposit/withdrawal) would otherwise
        # compute a phantom drawdown/loss and trip the circuit breakers on a
        # fresh, lossless session. When the basis is stale we rebuild the whole
        # session from the live equity.
        if abs(persisted_start - equity) > max(equity, 1.0) * 0.5:
            start_equity = equity
            peak_equity = equity
            realized_pnl_today = 0.0
            orders_today = 0
            last_order_at = None
        else:
            start_equity = persisted_start
            peak_equity = max(float(data.get("peak_equity", equity)), equity)
            realized_pnl_today = float(data.get("realized_pnl_today", 0.0))
            orders_today = int(data.get("orders_today", 0))
            last_order_at = datetime.fromisoformat(last_order) if last_order else None
        # Adaptive-risk state. Persisted so the streak/scale survives across the
        # per-cycle TradingEngine rebuilds the dashboard does (and across
        # restarts). Without this, adaptive risk was a permanent no-op because
        # every cycle started a brand-new RiskManager at scale 1.0.
        risk_scale = float(data.get("risk_scale", 1.0))
        win_streak = int(data.get("win_streak", 0))
        loss_streak = int(data.get("loss_streak", 0))
        return SessionState(
            start_equity=start_equity,
            peak_equity=peak_equity,
            current_equity=equity,
            realized_pnl_today=realized_pnl_today,
            orders_today=orders_today,
            last_order_at=last_order_at,
            risk_scale=risk_scale,
            win_streak=win_streak,
            loss_streak=loss_streak,
        )

    def save(self, state: SessionState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(state)
        payload["session_date"] = date.today().isoformat()
        payload["saved_at"] = datetime.now(timezone.utc).isoformat()
        payload["last_order_at"] = state.last_order_at.isoformat() if state.last_order_at else None
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


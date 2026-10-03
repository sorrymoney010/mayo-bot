from __future__ import annotations

import fcntl
import json
import threading
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path

from .risk import RiskManager, SessionState

# The fast exit watcher and the 300s sleeves both record a close into this
# file. flock only on the outermost acquire: a second fd in the same process
# deadlocks on Linux.
_SESSION_THREAD = threading.RLock()
_SESSION_DEPTH = threading.local()


@contextmanager
def session_lock(path: Path | str):
    """Exclusive lock for session_state.json. Reentrant on this thread."""
    depth = getattr(_SESSION_DEPTH, "n", 0)
    if depth:
        _SESSION_DEPTH.n = depth + 1
        try:
            yield
        finally:
            _SESSION_DEPTH.n = depth
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(target) + ".sessionlock")
    fh = open(lock_path, "a+", encoding="utf-8")
    _SESSION_DEPTH.n = 1
    try:
        with _SESSION_THREAD:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()
        _SESSION_DEPTH.n = 0


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

    def save(self, state: SessionState, *, keep_disk_accounting: bool = False) -> None:
        """Persist the session.

        ``keep_disk_accounting`` reloads realized P&L, streaks and the risk
        scale under the session lock and keeps those fields from the file.
        A sleeve that loaded at the start of the 300s cycle must not write
        its stale copy over a fast exit that landed mid-cycle. Closes
        themselves go through :func:`record_close_pnl`.
        """
        with session_lock(self.path):
            writing = state
            if keep_disk_accounting:
                writing = self._overlay_disk_accounting(state)
            self._write(writing)

    def _overlay_disk_accounting(self, state: SessionState) -> SessionState:
        if not self.path.exists():
            return state
        try:
            disk = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return state
        if not isinstance(disk, dict) or disk.get("session_date") != date.today().isoformat():
            return state
        return SessionState(
            start_equity=state.start_equity,
            peak_equity=max(float(state.peak_equity), float(disk.get("peak_equity") or 0.0)),
            current_equity=state.current_equity,
            realized_pnl_today=float(disk.get("realized_pnl_today") or 0.0),
            orders_today=state.orders_today,
            last_order_at=state.last_order_at,
            risk_scale=float(disk.get("risk_scale", state.risk_scale)),
            win_streak=int(disk.get("win_streak") or 0),
            loss_streak=int(disk.get("loss_streak") or 0),
        )

    def _write(self, state: SessionState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(state)
        payload["session_date"] = date.today().isoformat()
        payload["saved_at"] = datetime.now(timezone.utc).isoformat()
        payload["last_order_at"] = state.last_order_at.isoformat() if state.last_order_at else None
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


def record_close_pnl(path: Path | str, realized: float, equity: float, settings,
                     state: SessionState | None = None) -> SessionState:
    """Apply one close the way a sleeve does, under the session lock.

    Reloads the file first so a fast exit and a sleeve exit in the same
    minute both land in ``realized_pnl_today`` and the loss streak. The
    daily-loss halt reads that field.
    """
    path = Path(path)
    with session_lock(path):
        store = StateStore(path)
        fresh = store.load(float(equity))
        RiskManager(settings).update_scale_from_trade(float(realized), fresh)
        fresh.realized_pnl_today += float(realized)
        fresh.current_equity = float(equity)
        fresh.peak_equity = max(float(fresh.peak_equity), float(equity))
        if state is not None:
            fresh.orders_today = max(int(fresh.orders_today), int(state.orders_today))
            if state.last_order_at is not None and (
                fresh.last_order_at is None or state.last_order_at > fresh.last_order_at
            ):
                fresh.last_order_at = state.last_order_at
        store._write(fresh)
        if state is not None:
            state.start_equity = fresh.start_equity
            state.peak_equity = fresh.peak_equity
            state.current_equity = fresh.current_equity
            state.realized_pnl_today = fresh.realized_pnl_today
            state.risk_scale = fresh.risk_scale
            state.win_streak = fresh.win_streak
            state.loss_streak = fresh.loss_streak
        return fresh


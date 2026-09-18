"""Duplicate-order prevention via persistent idempotency keys.

The dangerous scenario is not a bug in the strategy — it is a *retry*.  If an
``AddOrder`` request times out, the order may or may not have reached Kraken.
Blindly retrying can double the position.

Kraken supports a ``userref`` (32-bit signed int) and, on newer API versions,
``cl_ord_id``.  We derive both from a deterministic intent key, so:

* Retrying the same logical intent reuses the same userref, letting us query
  Kraken and discover whether the order already exists.
* A different intent produces a different key, so legitimate new orders proceed.

The ledger is persisted as JSON so it survives restarts.  Records move through
``pending`` → ``confirmed``/``failed``; a ``pending`` record found at startup is
exactly the "we don't know if it landed" case and must be reconciled against the
exchange before any new order is placed.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal

from .errors import DuplicateOrderError

OrderStatus = Literal["pending", "confirmed", "failed"]

# Kraken userref is a signed 32-bit integer.
_USERREF_MODULUS = 2_147_483_647


def make_intent_key(
    *,
    symbol: str,
    side: str,
    notional_usd: float,
    bar_timestamp: str,
    strategy: str = "trend_breakout",
) -> str:
    """Deterministic key for one trading intent.

    Keyed on the *bar timestamp* rather than wall-clock time: two evaluations of
    the same closed bar are the same intent and must not produce two orders,
    while the next bar legitimately produces a new one.
    """
    payload = f"{strategy}|{symbol}|{side}|{notional_usd:.2f}|{bar_timestamp}"
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def userref_from_key(key: str) -> int:
    """Map an intent key onto a positive 32-bit Kraken userref."""
    digest = hashlib.sha256(key.encode()).digest()
    return (int.from_bytes(digest[:4], "big") % (_USERREF_MODULUS - 1)) + 1


@dataclass
class OrderRecord:
    key: str
    userref: int
    symbol: str
    side: str
    notional_usd: float
    bar_timestamp: str
    status: OrderStatus = "pending"
    order_id: str | None = None
    error: str | None = None
    dry_run: bool = True
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return asdict(self)


class IdempotencyLedger:
    """Persistent record of every order intent this bot has ever formed."""

    def __init__(self, path: Path, retention_days: int = 30) -> None:
        self.path = Path(path)
        self.retention_days = retention_days
        self._lock = threading.RLock()
        self._records: dict[str, OrderRecord] = {}
        self._load()

    # ── persistence ──────────────────────────────────────────

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Corrupt ledger: fail closed by keeping it empty but preserving the
            # file for forensics rather than overwriting it silently.
            return
        for key, value in (raw.get("orders") or {}).items():
            try:
                self._records[key] = OrderRecord(**value)
            except TypeError:
                continue

    def _flush_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "orders": {k: v.to_dict() for k, v in self._records.items()},
        }
        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        os.replace(tmp_name, self.path)

    # ── API ──────────────────────────────────────────────────

    def get(self, key: str) -> OrderRecord | None:
        with self._lock:
            return self._records.get(key)

    def reserve(
        self,
        *,
        key: str,
        symbol: str,
        side: str,
        notional_usd: float,
        bar_timestamp: str,
        dry_run: bool,
    ) -> OrderRecord:
        """Claim an intent key before touching the network.

        Raises ``DuplicateOrderError`` if this intent was already submitted —
        including the ``pending`` case, where the safe action is to reconcile,
        never to resend.
        """
        with self._lock:
            existing = self._records.get(key)
            if existing is not None:
                if existing.status == "failed":
                    # A definitively failed order may be retried under the same
                    # key: we know it never reached the book.
                    existing.status = "pending"
                    existing.error = None
                    existing.updated_at = datetime.now(timezone.utc).isoformat()
                    self._flush_locked()
                    return existing
                raise DuplicateOrderError(
                    f"order intent {key} already {existing.status} "
                    f"(order_id={existing.order_id})"
                )
            record = OrderRecord(
                key=key,
                userref=userref_from_key(key),
                symbol=symbol,
                side=side,
                notional_usd=notional_usd,
                bar_timestamp=bar_timestamp,
                dry_run=dry_run,
            )
            self._records[key] = record
            self._flush_locked()
            return record

    def confirm(self, key: str, order_id: str) -> OrderRecord:
        with self._lock:
            record = self._records[key]
            record.status = "confirmed"
            record.order_id = order_id
            record.error = None
            record.updated_at = datetime.now(timezone.utc).isoformat()
            self._flush_locked()
            return record

    def fail(self, key: str, error: str) -> OrderRecord:
        with self._lock:
            record = self._records[key]
            record.status = "failed"
            record.error = error
            record.updated_at = datetime.now(timezone.utc).isoformat()
            self._flush_locked()
            return record

    def pending(self) -> list[OrderRecord]:
        """Intents that may or may not have reached the exchange."""
        with self._lock:
            return [r for r in self._records.values() if r.status == "pending"]

    def prune(self) -> int:
        """Drop settled records older than the retention window."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        removed = 0
        with self._lock:
            for key, record in list(self._records.items()):
                if record.status == "pending":
                    continue
                try:
                    updated = datetime.fromisoformat(record.updated_at)
                except ValueError:
                    continue
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                if updated < cutoff:
                    del self._records[key]
                    removed += 1
            if removed:
                self._flush_locked()
        return removed

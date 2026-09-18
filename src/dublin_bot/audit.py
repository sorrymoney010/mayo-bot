"""Append-only, hash-chained audit log.

Every safety-relevant event — order intents, rejections, safety-lock states,
freshness failures, config snapshots — is written here as one JSON object per
line.  Two properties matter:

* **Append-only.** The file is opened in ``"a"`` mode and flushed per record, so
  a crash mid-cycle cannot lose earlier entries.
* **Tamper-evident.** Each entry carries the SHA-256 of the previous entry, so
  deleting or editing a line breaks the chain and ``verify_chain`` detects it.
  This is what makes the log usable as evidence in a post-incident review.

Secrets are never logged: ``_SENSITIVE_KEYS`` values are replaced with
``"[REDACTED]"`` recursively before serialization.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import fcntl
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterator

GENESIS_HASH = "0" * 64

_PATH_LOCKS: dict[Path, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _path_lock(path: Path) -> threading.Lock:
    """Return one in-process lock for every physical audit path."""
    resolved = path.resolve()
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(resolved, threading.Lock())

_SENSITIVE_KEYS = frozenset({
    "api_key", "api_secret", "apikey", "apisecret", "secret", "password",
    "token", "kraken_api_key", "kraken_api_secret", "alpaca_api_key",
    "alpaca_api_secret", "api-key", "api-sign", "authorization", "nonce",
})


class AuditEvent(StrEnum):
    STARTUP = "startup"
    SHUTDOWN = "shutdown"
    CONFIG_SNAPSHOT = "config_snapshot"
    SAFETY_CHECK = "safety_check"
    SAFETY_VIOLATION = "safety_violation"
    DATA_FRESHNESS = "data_freshness"
    MARKET_QUALITY = "market_quality"
    SIGNAL = "signal"
    RISK_DECISION = "risk_decision"
    ORDER_INTENT = "order_intent"
    ORDER_SUBMITTED = "order_submitted"
    ORDER_FILLED = "order_filled"
    ORDER_REJECTED = "order_rejected"
    ORDER_DUPLICATE_BLOCKED = "order_duplicate_blocked"
    ORDER_CANCELLED = "order_cancelled"
    ORDER_EDITED = "order_edited"
    RECOVERY = "recovery"
    RECONCILIATION = "reconciliation"
    BROKER_ERROR = "broker_error"
    RATE_LIMIT = "rate_limit"


def redact(value: Any) -> Any:
    """Recursively strip credential-shaped values from a payload."""
    if isinstance(value, dict):
        return {
            k: ("[REDACTED]" if str(k).lower() in _SENSITIVE_KEYS else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def _entry_hash(entry: dict) -> str:
    """Hash of an entry excluding its own ``entry_hash`` field."""
    material = {k: v for k, v in entry.items() if k != "entry_hash"}
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class AuditLog:
    """Hash-chained JSONL audit trail."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = _path_lock(self.path)
        self._last_hash = self._read_last_hash()

    def _read_last_hash(self) -> str:
        if not self.path.exists():
            return GENESIS_HASH
        last = GENESIS_HASH
        try:
            with self.path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        last = json.loads(line).get("entry_hash", last)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return GENESIS_HASH
        return last

    def record(self, event: AuditEvent | str, payload: dict | None = None,
               *, severity: str = "info") -> dict:
        """Append one entry and return it (with its computed hash).

        The last hash is re-read while holding both a per-path thread lock and
        an OS file lock. Dashboard requests create several ``AuditLog``
        instances, and without these locks two writers can use the same parent
        hash and permanently fork the chain.
        """
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    handle.seek(0)
                    last_hash = GENESIS_HASH
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            last_hash = json.loads(line).get("entry_hash", last_hash)
                        except json.JSONDecodeError:
                            continue
                    entry = {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "event": str(event),
                        "severity": severity,
                        "payload": redact(payload or {}),
                        "previous_hash": last_hash,
                    }
                    entry["entry_hash"] = _entry_hash(entry)
                    handle.seek(0, os.SEEK_END)
                    handle.write(json.dumps(entry, sort_keys=True) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                    self._last_hash = entry["entry_hash"]
                    return entry
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def entries(self) -> Iterator[dict]:
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def tail(self, limit: int = 50) -> list[dict]:
        return list(self.entries())[-limit:]

    def verify_chain(self) -> tuple[bool, str]:
        """Confirm the hash chain is intact. Returns (ok, human-readable reason)."""
        previous = GENESIS_HASH
        count = 0
        for index, entry in enumerate(self.entries()):
            count += 1
            if entry.get("previous_hash") != previous:
                return False, f"chain break at entry {index}: previous_hash mismatch"
            expected = _entry_hash(entry)
            if entry.get("entry_hash") != expected:
                return False, f"tampered entry {index}: content hash mismatch"
            previous = entry["entry_hash"]
        return True, f"chain intact across {count} entries"

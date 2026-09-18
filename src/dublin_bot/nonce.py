"""Strictly monotonic nonce generation for Kraken private endpoints.

Kraken rejects any private request whose nonce is not strictly greater than the
previous nonce seen for that API key.  Two failure modes must be prevented:

1. **Collisions within a process.** ``int(time.time() * 1000)`` repeats when two
   calls land in the same millisecond, so we track the last issued value and
   always advance by at least one.

2. **Regression across restarts.** If the process restarts and the clock has
   drifted backwards (NTP correction, sleep/wake on a laptop), a fresh
   time-derived nonce can be *lower* than one already used, permanently locking
   the key out until the counter catches up.  We persist the high-water mark to
   disk and resume above it.

Nonces are microsecond-resolution to leave headroom for bursts.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import fcntl
from pathlib import Path


_PATH_LOCKS: dict[Path, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _path_lock(path: Path) -> threading.Lock:
    resolved = path.resolve()
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(resolved, threading.Lock())


class NonceGenerator:
    """Thread-safe, restart-safe, strictly increasing nonce source."""

    def __init__(self, state_path: Path | None = None) -> None:
        self._state_path = Path(state_path) if state_path else None
        self._lock = (
            _path_lock(self._state_path)
            if self._state_path is not None
            else threading.Lock()
        )
        self._last = self._load_persisted()

    def _load_persisted(self) -> int:
        if self._state_path is None or not self._state_path.exists():
            return 0
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return int(data.get("last_nonce", 0))
        except (OSError, ValueError, json.JSONDecodeError):
            # A corrupt file must not brick the bot; time-based nonces will
            # still be far above zero in practice.
            return 0

    def _persist(self, value: int) -> None:
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(self._state_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"last_nonce": value}, handle)
            os.replace(tmp_name, self._state_path)
        except OSError:
            # Persistence is best-effort; in-process monotonicity still holds.
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def next(self) -> int:
        """Return a nonce strictly greater than every nonce previously issued.

        Uses **nanosecond** resolution.  Kraken tracks the maximum nonce ever
        seen for an API key and rejects anything lower, so once a higher value
        has been observed the counter must stay above it.  Microsecond-scale
        values are unsafe because a single high-resolution call (e.g. a raw
        diagnostic using ``time()*1e9``) permanently raises the server's
        watermark above every microsecond nonce the bot would otherwise issue,
        producing a permanent ``EAPI:Invalid nonce`` lockout.
        """
        with self._lock:
            if self._state_path is None:
                candidate = max(int(time.time() * 1_000_000_000), self._last + 1)
                self._last = candidate
                return candidate

            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self._state_path.with_suffix(self._state_path.suffix + ".lock")
            with lock_path.open("a+", encoding="utf-8") as lock_handle:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                try:
                    persisted = self._load_persisted()
                    candidate = max(
                        int(time.time() * 1_000_000_000),
                        self._last + 1,
                        persisted + 1,
                    )
                    self._last = candidate
                    self._persist(candidate)
                    return candidate
                finally:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    @property
    def last(self) -> int:
        return self._last

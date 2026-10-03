"""Cross-sleeve symbol ownership for the PAPER loop.

Several paper sleeves (e.g. the primary ``regime_trend@60m`` engine and the
``meanrev_4h`` sleeve) share one paper book (``logs/paper_portfolio.json``)
and one bot-owned lot ledger (``logs/paper_bot_positions.json``). This small
registry records which sleeve owns a held lot and which sleeve has a resting
(pending) paper limit order, so that:

* a sleeve never enters a symbol another sleeve holds or has an order on,
* a sleeve never exits (or applies its stop/TP to) another sleeve's lot,
* pending orders count toward the shared max-positions and exposure caps.

Lots with no registry entry belong to the PRIMARY engine (backward compatible
with lots opened before the registry existed). Paper/dry-run only: the live
path never reads this file.
"""
from __future__ import annotations

import fcntl
import json
import threading
from contextlib import contextmanager
from pathlib import Path

PRIMARY = "primary"
DEFAULT_PATH = Path("logs/paper_sleeve_owners.json")

# The fast exit watcher releases a symbol the moment the lot is flat. A sleeve
# rewrites its slice at the end of the 300s cycle. Both have to take this lock
# or the sleeve's stale snapshot puts the symbol back.
_REG_THREAD = threading.RLock()
_REG_DEPTH = threading.local()


@contextmanager
def registry_lock(path: Path | str = DEFAULT_PATH):
    """Exclusive lock for the owners file. Reentrant on this thread."""
    depth = getattr(_REG_DEPTH, "n", 0)
    if depth:
        _REG_DEPTH.n = depth + 1
        try:
            yield
        finally:
            _REG_DEPTH.n = depth
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(target) + ".reglock")
    fh = open(lock_path, "a+", encoding="utf-8")
    _REG_DEPTH.n = 1
    try:
        with _REG_THREAD:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()
        _REG_DEPTH.n = 0


def release_symbol(symbol: str, *, path: Path | str = DEFAULT_PATH) -> None:
    """Drop ``symbol`` from the owners file. The lot is flat."""
    with registry_lock(path):
        reg = SleeveRegistry(path)
        reg.owners.pop(str(symbol).upper(), None)
        reg.save()


class SleeveRegistry:
    def __init__(self, path: Path | str = DEFAULT_PATH) -> None:
        self.path = Path(path)
        self.owners: dict[str, str] = {}
        self.pending: dict[str, dict] = {}
        self.load()

    # ── persistence ────────────────────────────────────────────
    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        self.owners = {str(k).upper(): str(v) for k, v in (data.get("owners") or {}).items()}
        self.pending = {
            str(k).upper(): dict(v) for k, v in (data.get("pending") or {}).items()
            if isinstance(v, dict)
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"owners": self.owners, "pending": self.pending},
                                  indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    # ── queries ────────────────────────────────────────────────
    def owner_of(self, symbol: str) -> str:
        return self.owners.get(symbol.upper(), PRIMARY)

    def foreign_symbols(self, sleeve: str) -> set[str]:
        """Symbols held OR with a pending order by any sleeve other than ``sleeve``."""
        out = {s for s, o in self.owners.items() if o != sleeve}
        out |= {s for s, p in self.pending.items() if p.get("sleeve") != sleeve}
        return out

    def foreign_pending(self, sleeve: str) -> dict[str, dict]:
        return {s: p for s, p in self.pending.items() if p.get("sleeve") != sleeve}

    def pending_notional(self, sleeve: str | None = None) -> float:
        return sum(float(p.get("notional", 0.0) or 0.0) for p in self.pending.values()
                   if sleeve is None or p.get("sleeve") == sleeve)

    # ── mutations (caller saves) ───────────────────────────────
    def set_sleeve(self, sleeve: str, *, owned: set[str], pending: dict[str, float]) -> None:
        """Replace ``sleeve``'s entries with its authoritative state."""
        self.owners = {s: o for s, o in self.owners.items() if o != sleeve}
        self.pending = {s: p for s, p in self.pending.items() if p.get("sleeve") != sleeve}
        for sym in owned:
            self.owners[sym.upper()] = sleeve
        for sym, notional in pending.items():
            self.pending[sym.upper()] = {"sleeve": sleeve, "notional": float(notional)}

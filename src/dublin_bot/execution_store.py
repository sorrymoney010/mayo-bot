"""Durable matching of strategy signals to Kraken order fills."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class ExecutionFill:
    order_id: str
    symbol: str
    side: str
    status: str
    signal_price: float
    fill_price: float
    slippage_usd: float
    slippage_bps: float
    volume: float
    cost: float
    fee: float
    filled_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class ExecutionStore:
    """SQLite execution ledger keyed by Kraken order transaction id."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS executions (
                    order_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    status TEXT NOT NULL,
                    signal_price REAL NOT NULL,
                    fill_price REAL NOT NULL,
                    slippage_usd REAL NOT NULL,
                    slippage_bps REAL NOT NULL,
                    volume REAL NOT NULL,
                    cost REAL NOT NULL,
                    fee REAL NOT NULL,
                    filled_at TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def save(self, fill: ExecutionFill) -> None:
        values = fill.to_dict()
        columns = tuple(values)
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(f"{name}=excluded.{name}" for name in columns[1:])
        with self._connect() as db:
            db.execute(
                f"INSERT INTO executions ({', '.join(columns)}) VALUES ({placeholders}) "
                f"ON CONFLICT(order_id) DO UPDATE SET {updates}",
                tuple(values[name] for name in columns),
            )

    def get(self, order_id: str) -> ExecutionFill | None:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM executions WHERE order_id = ?", (order_id,)
            ).fetchone()
        return ExecutionFill(**dict(row)) if row else None

    def recent(self, limit: int = 100) -> list[ExecutionFill]:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM executions ORDER BY filled_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [ExecutionFill(**dict(row)) for row in rows]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

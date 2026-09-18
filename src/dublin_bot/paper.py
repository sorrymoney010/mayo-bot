"""Paper portfolio state: positions, cost basis, realized/unrealized P&L.

This is intentionally independent from SessionState so that live trading can
continue to use the existing risk state, while paper trading gets realistic
accounting without changing broker behavior.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock


@dataclass
class Position:
    symbol: str
    quantity: float = 0.0
    entry_price: float = 0.0
    fees_paid: float = 0.0
    opened_at: str = ""
    trades: int = 0

    @property
    def cost_basis(self) -> float:
        return self.quantity * self.entry_price + self.fees_paid


@dataclass
class Portfolio:
    equity: float = 0.0
    cash: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    updated_at: str = ""

    def to_dict(self) -> dict[str, object]:
        positions: list[dict[str, object]] = [
            {
                "symbol": p.symbol,
                "quantity": p.quantity,
                "entry_price": p.entry_price,
                "market_value": 0.0,
                "unrealized_pl": 0.0,
                "fees_paid": p.fees_paid,
                "trades": p.trades,
                "opened_at": p.opened_at,
            }
            for p in self.positions.values()
        ]
        return {
            "equity": self.equity,
            "cash": self.cash,
            "positions": positions,
        }


class PaperPortfolio:
    """Persisted paper-trading portfolio state."""

    def __init__(self, path: Path | str = Path("logs/paper_portfolio.json")) -> None:
        self.path = Path(path)
        self._lock = Lock()
        self._portfolio = Portfolio()

    def load(self, equity: float, cash: float) -> Portfolio:
        with self._lock:
            if self.path.exists():
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                    self._portfolio = Portfolio(
                        equity=float(data.get("equity", equity)),
                        cash=float(data.get("cash", cash)),
                        updated_at=data.get("updated_at", ""),
                    )
                    self._portfolio.positions = {
                        pos["symbol"]: Position(
                            symbol=pos["symbol"],
                            quantity=float(pos.get("quantity", 0.0)),
                            entry_price=float(pos.get("entry_price", 0.0)),
                            fees_paid=float(pos.get("fees_paid", 0.0)),
                            opened_at=pos.get("opened_at", ""),
                            trades=int(pos.get("trades", 0)),
                        )
                        for pos in data.get("positions", [])
                        if float(pos.get("quantity", 0.0)) > 1e-12
                    }
                except (OSError, json.JSONDecodeError, ValueError):
                    self._portfolio = Portfolio(equity=equity, cash=cash)
            else:
                self._portfolio = Portfolio(equity=equity, cash=cash)
            return Portfolio(
                equity=self._portfolio.equity,
                cash=self._portfolio.cash,
                updated_at=self._portfolio.updated_at,
                positions={
                    k: Position(
                        symbol=p.symbol,
                        quantity=p.quantity,
                        entry_price=p.entry_price,
                        fees_paid=p.fees_paid,
                        opened_at=p.opened_at,
                        trades=p.trades,
                    )
                    for k, p in self._portfolio.positions.items()
                },
            )

    def save(self) -> None:
        with self._lock:
            self._save_unlocked()

    def _save_unlocked(self) -> None:
        self._portfolio.updated_at = datetime.now(timezone.utc).isoformat()
        payload = {
            "equity": self._portfolio.equity,
            "cash": self._portfolio.cash,
            "updated_at": self._portfolio.updated_at,
            "positions": [
                {
                    "symbol": p.symbol,
                    "quantity": p.quantity,
                    "entry_price": p.entry_price,
                    "fees_paid": p.fees_paid,
                    "opened_at": p.opened_at,
                    "trades": p.trades,
                }
                for p in self._portfolio.positions.values()
            ],
        }
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)

    def snapshot(self) -> Portfolio:
        with self._lock:
            portfolio = Portfolio(
                equity=self._portfolio.equity,
                cash=self._portfolio.cash,
                updated_at=self._portfolio.updated_at,
            )
            portfolio.positions = {
                k: Position(
                    symbol=p.symbol,
                    quantity=p.quantity,
                    entry_price=p.entry_price,
                    fees_paid=p.fees_paid,
                    opened_at=p.opened_at,
                    trades=p.trades,
                )
                for k, p in self._portfolio.positions.items()
            }
            return portfolio

    def mark_to_market(self, prices: dict[str, float]) -> Portfolio:
        portfolio = self.snapshot()
        positions = []
        for position in portfolio.positions.values():
            market_value = position.quantity * prices.get(position.symbol, position.entry_price)
            unrealized_pl = market_value - position.cost_basis
            positions.append({
                "symbol": position.symbol,
                "quantity": position.quantity,
                "entry_price": position.entry_price,
                "market_value": market_value,
                "unrealized_pl": unrealized_pl,
                "fees_paid": position.fees_paid,
                "trades": position.trades,
                "opened_at": position.opened_at,
            })
        portfolio.positions.clear()
        for pos in positions:
            symbol = pos["symbol"]
            portfolio.positions[symbol] = Position(
                symbol=symbol,
                quantity=pos["quantity"],
                entry_price=pos["entry_price"],
                fees_paid=pos["fees_paid"],
                opened_at=pos["opened_at"],
                trades=pos["trades"],
            )
        return portfolio

    def record_buy(self, symbol: str, quantity: float, fill_price: float, fee: float, when: str) -> None:
        with self._lock:
            if symbol not in self._portfolio.positions:
                self._portfolio.positions[symbol] = Position(symbol=symbol, opened_at=when)
            position = self._portfolio.positions[symbol]
            # Always add to quantity first
            position.quantity += quantity
            if position.quantity <= 1e-12:
                # Should not happen after adding, but safety check
                position.entry_price = fill_price
                position.fees_paid = fee
            else:
                # For additional buys, update weighted average entry
                old_cost = (position.quantity - quantity) * position.entry_price + position.fees_paid
                new_cost = quantity * fill_price + fee
                position.entry_price = (old_cost + new_cost) / position.quantity
                position.fees_paid += fee
            position.trades += 1
            self._portfolio.equity -= (quantity * fill_price + fee)
            self._portfolio.cash -= (quantity * fill_price + fee)
            self._save_unlocked()

    def record_sell(self, symbol: str, quantity: float, fill_price: float, fee: float, when: str) -> float:
        with self._lock:
            position = self._portfolio.positions.get(symbol)
            if position is None or position.quantity <= 1e-12:
                raise ValueError(f"No open paper position for {symbol}")
            close_quantity = min(quantity, position.quantity)
            cost_basis = (close_quantity / max(position.quantity, 1e-12)) * (position.quantity * position.entry_price + position.fees_paid)
            proceeds = close_quantity * fill_price - fee
            realized_pl = proceeds - cost_basis
            position.quantity -= close_quantity
            position.fees_paid += fee
            position.trades += 1
            self._portfolio.equity += proceeds
            self._portfolio.cash += proceeds
            if position.quantity <= 1e-12:
                del self._portfolio.positions[symbol]
            self._save_unlocked()
            return realized_pl

    def record_trade(
        self,
        symbol: str,
        action: str,
        price: float,
        quantity: float,
        pnl: float = 0.0,
    ) -> None:
        """Simplified record_trade for the trading agent."""
        if action.upper() == "BUY":
            self.record_buy(symbol, quantity, price, 0.0, datetime.now(timezone.utc).isoformat())
        elif action.upper() == "SELL":
            self.record_sell(symbol, quantity, price, 0.0, datetime.now(timezone.utc).isoformat())

    def holdings(self) -> dict[str, float]:
        """Return current holdings as {symbol: quantity}."""
        with self._lock:
            return {
                symbol: position.quantity
                for symbol, position in self._portfolio.positions.items()
                if position.quantity > 1e-12
            }

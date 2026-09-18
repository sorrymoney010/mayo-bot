from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path

from .validation import BacktestResult


class LiveReadinessReport:
    def __init__(self, minimum_trades: int = 30, max_drawdown: float = 0.10, min_profit_factor: float = 1.10) -> None:
        self.minimum_trades = minimum_trades
        self.max_drawdown = max_drawdown
        self.min_profit_factor = min_profit_factor

    def assess(self, folds: list[BacktestResult]) -> dict[str, object]:
        profitable_folds = sum(result.total_return > 0 for result in folds)
        total_trades = sum(result.trades for result in folds)
        worst_drawdown = max((result.max_drawdown for result in folds), default=1.0)
        finite_factors = [result.profit_factor for result in folds if result.profit_factor != float("inf")]
        average_profit_factor = sum(finite_factors) / len(finite_factors) if finite_factors else 0.0
        checks = {
            "enough_trades": total_trades >= self.minimum_trades,
            "majority_profitable_folds": profitable_folds > len(folds) / 2,
            "drawdown_within_limit": worst_drawdown <= self.max_drawdown,
            "profit_factor_passed": average_profit_factor >= self.min_profit_factor,
        }
        return {
            "live_ready": all(checks.values()),
            "checks": checks,
            "folds": [asdict(result) for result in folds],
            "summary": {
                "total_trades": total_trades,
                "profitable_folds": profitable_folds,
                "worst_drawdown": worst_drawdown,
                "average_profit_factor": average_profit_factor,
            },
        }

    @staticmethod
    def write(report: dict[str, object], path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")


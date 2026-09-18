from __future__ import annotations

from dataclasses import dataclass
import math
import pandas as pd

from .config import Settings
from .models import Action
from .strategy import build_strategy


@dataclass(frozen=True)
class BacktestCosts:
    fee_bps: float = 15.0
    spread_bps: float = 10.0
    slippage_bps: float = 5.0

    @property
    def one_way_fraction(self) -> float:
        return (self.fee_bps + self.spread_bps / 2 + self.slippage_bps) / 10_000


@dataclass(frozen=True)
class BacktestResult:
    starting_equity: float
    ending_equity: float
    total_return: float
    max_drawdown: float
    trades: int
    wins: int
    losses: int
    profit_factor: float


def backtest(bars: pd.DataFrame, settings: Settings, costs: BacktestCosts | None = None) -> BacktestResult:
    costs = costs or BacktestCosts()
    strategy = build_strategy(settings)
    equity = settings.strategy_equity_usd
    peak = equity
    max_dd = 0.0
    entry = 0.0
    notional = 0.0
    stop = 0.0
    wins = losses = trades = 0
    gross_wins = gross_losses = 0.0

    warmup = max(settings.regime_ema, settings.lookback_bars // 2)
    for end in range(warmup, len(bars)):
        window = bars.iloc[: end + 1]
        in_position = notional > 0
        signal = strategy.evaluate(window, in_position=in_position)
        price = float(window.iloc[-1]["close"])
        if not in_position and signal.action is Action.BUY and signal.stop_price:
            entry = price * (1 + costs.one_way_fraction)
            stop = signal.stop_price
            stop_fraction = max((entry - stop) / entry, 0.001)
            notional = min(
                settings.strategy_equity_usd * settings.max_position_fraction,
                settings.strategy_equity_usd * settings.risk_per_trade / stop_fraction,
                equity,
            )
        elif in_position and (price <= stop or signal.action is Action.SELL):
            exit_price = price * (1 - costs.one_way_fraction)
            pnl = notional * ((exit_price - entry) / entry)
            equity += pnl
            trades += 1
            if pnl >= 0:
                wins += 1
                gross_wins += pnl
            else:
                losses += 1
                gross_losses += abs(pnl)
            notional = 0.0
            peak = max(peak, equity)
            max_dd = max(max_dd, 1 - equity / max(peak, 1e-9))

    profit_factor = gross_wins / gross_losses if gross_losses else (math.inf if gross_wins else 0.0)
    return BacktestResult(
        starting_equity=settings.strategy_equity_usd,
        ending_equity=round(equity, 4),
        total_return=(equity / settings.strategy_equity_usd) - 1,
        max_drawdown=max_dd,
        trades=trades,
        wins=wins,
        losses=losses,
        profit_factor=profit_factor,
    )


def walk_forward(bars: pd.DataFrame, settings: Settings, folds: int = 4) -> list[BacktestResult]:
    if folds < 2:
        raise ValueError("folds must be at least 2")
    minimum = max(settings.regime_ema + 50, 250)
    usable = len(bars) - minimum
    if usable <= folds:
        raise ValueError("not enough bars for walk-forward validation")
    fold_size = usable // folds
    results: list[BacktestResult] = []
    for index in range(folds):
        test_start = minimum + index * fold_size
        test_end = len(bars) if index == folds - 1 else test_start + fold_size
        evaluation = bars.iloc[max(0, test_start - minimum):test_end]
        results.append(backtest(evaluation, settings))
    return results


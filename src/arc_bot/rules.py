"""Prop-firm rule model and guard (DayTraders 25K EOD eval, as published on their site).

Rules are data, not code: verify against the firm's current rules page before use.
Unknown details (drawdown lock level, consistency base) are explicit config fields.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PropRules:
    start_balance: float = 25_000.0
    profit_target: float = 1_000.0
    trailing_drawdown: float = 800.0       # EOD trailing: floor trails the highest EOD balance
    lock_floor_at_start: bool = True       # assumption: floor stops trailing once it hits start balance
    daily_loss_limit: float = 1_500.0      # firm limit (we use a much tighter own limit)
    max_micros: int = 60                   # "6 contracts [60 micros]"
    min_qualifying_days: int = 2
    min_daily_profit: float = 100.0        # a "qualifying day" needs >= this
    consistency_pct: float = 0.50          # best day must be <= 50% of total profit (eval)


@dataclass
class Instrument:
    symbol: str
    point_value: float      # $ per 1.00 index point per contract
    tick: float
    micros_per_contract: int = 1   # micro contracts already count as 1
    fee_per_side: float = 0.62     # commission+exchange per contract per side (assumption)


MES = Instrument("MES", 5.0, 0.25)
MNQ = Instrument("MNQ", 2.0, 0.25)
INSTRUMENTS = {"MES": MES, "MNQ": MNQ}


@dataclass
class PropGuard:
    """Tracks account state and vetoes risk that could breach the firm's rules."""

    rules: PropRules = field(default_factory=PropRules)
    own_daily_stop: float = 300.0     # our self-imposed daily loss stop (<< firm limit)
    own_daily_target: float = 450.0   # stop trading once hit (protects consistency rule)
    eod_balance: float = 0.0
    peak_eod: float = 0.0
    day_pnl: float = 0.0
    day_pnls: list[float] = field(default_factory=list)
    blown: bool = False

    def __post_init__(self) -> None:
        self.eod_balance = self.peak_eod = self.rules.start_balance

    @property
    def floor(self) -> float:
        f = self.peak_eod - self.rules.trailing_drawdown
        if self.rules.lock_floor_at_start:
            f = min(f, self.rules.start_balance)
        return f

    def cushion(self) -> float:
        """Dollars of equity we can lose before the account breaches the trailing floor."""
        return self.eod_balance + self.day_pnl - self.floor

    def can_trade(self) -> bool:
        if self.blown:
            return False
        if self.day_pnl <= -self.own_daily_stop or self.day_pnl >= self.own_daily_target:
            return False
        return self.cushion() > 0

    def max_risk_now(self, per_trade_risk: float) -> float:
        """Risk budget for the next trade: never more than half the remaining cushion."""
        room_daily = self.own_daily_stop + self.day_pnl
        return max(0.0, min(per_trade_risk, room_daily, self.cushion() * 0.5))

    def mark_equity(self, open_pnl: float) -> None:
        """Intraday check including unrealized P&L."""
        if self.eod_balance + self.day_pnl + open_pnl <= self.floor:
            self.blown = True
        if self.day_pnl + open_pnl <= -self.rules.daily_loss_limit:
            self.blown = True

    def realize(self, pnl: float) -> None:
        self.day_pnl += pnl

    def end_day(self) -> float:
        p = self.day_pnl
        self.day_pnls.append(p)
        self.eod_balance += p
        self.peak_eod = max(self.peak_eod, self.eod_balance)
        self.day_pnl = 0.0
        return p

    def contracts_ok(self, n: int) -> bool:
        return 0 < n <= self.rules.max_micros

    # ---- eval status ----
    def total_profit(self) -> float:
        return self.eod_balance - self.rules.start_balance

    def qualifying_days(self) -> int:
        return sum(1 for p in self.day_pnls if p >= self.rules.min_daily_profit)

    def consistency_ok(self) -> bool:
        tot = self.total_profit()
        if tot <= 0 or not self.day_pnls:
            return False
        return max(self.day_pnls) <= self.rules.consistency_pct * tot

    def passed(self) -> bool:
        r = self.rules
        return (not self.blown and self.total_profit() >= r.profit_target
                and self.qualifying_days() >= r.min_qualifying_days and self.consistency_ok())

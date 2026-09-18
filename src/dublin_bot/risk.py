from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd

from .config import Settings
from .models import Action, RiskDecision, Signal
from .sizing import PositionSizer, SizingConfig


@dataclass
class SessionState:
    start_equity: float
    peak_equity: float
    current_equity: float
    realized_pnl_today: float = 0.0
    orders_today: int = 0
    last_order_at: datetime | None = None
    # Adaptive-risk multiplier, persisted across cycles in the same session.
    risk_scale: float = 1.0
    win_streak: int = 0
    loss_streak: int = 0


class RiskManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.sizer = PositionSizer(
            SizingConfig(
                target_vol=settings.target_vol,
                kelly_fraction=settings.kelly_fraction,
                max_risk_per_trade=settings.risk_per_trade,
                max_leverage=settings.max_leverage,
            )
        )

    def effective_risk_per_trade(self, state: "SessionState | None" = None) -> float:
        """Base risk scaled by the session's win/loss adaptation factor.

        The scale is persisted on ``state`` (see ``state.py``) so it survives the
        per-cycle ``TradingEngine`` rebuilds the dashboard performs — a fresh
        ``RiskManager`` per cycle would otherwise always see scale 1.0 and
        adaptive risk would be a no-op. When ``state`` carries a scale, we trust
        the persisted value; otherwise we fall back to the in-memory ``_last_scale``.
        """
        s = self.settings
        if not s.adaptive_risk:
            return s.risk_per_trade
        scale = state.risk_scale if state is not None else self._last_scale
        return min(
            s.max_risk_scale,
            max(s.min_risk_scale, s.risk_per_trade * scale),
        )

    _last_scale = 1.0

    def update_scale_from_trade(self, realized_pnl: float, state: SessionState) -> None:
        """Adjust the adaptation factor from a single closed trade's P&L.

        A win nudges the scale up by ``risk_step``; a loss nudges it down.
        The scale is clamped to [min_risk_scale, max_risk_scale] so a hot streak
        compounds allocation while a cold streak tightens it. Win/loss streaks
        are persisted on ``state`` so the adaptation survives across cycles and
        restarts (previously these fields existed but were never written, so
        adaptive risk was a permanent no-op). The in-memory ``_last_scale`` is
        also kept in sync for the legacy API.
        """
        s = self.settings
        if not s.adaptive_risk:
            return
        # Trust the persisted scale as the source of truth (it survives
        # TradingEngine rebuilds); apply the delta on top of it.
        self._last_scale = state.risk_scale
        if realized_pnl > 0:
            state.win_streak += 1
            state.loss_streak = 0
            self._last_scale = min(s.max_risk_scale, self._last_scale + s.risk_step)
        elif realized_pnl < 0:
            state.loss_streak += 1
            state.win_streak = 0
            self._last_scale = max(s.min_risk_scale, self._last_scale - s.risk_step)
        else:
            return
        state.risk_scale = self._last_scale

    def update_scale(self, state: SessionState) -> None:
        """Legacy entry point kept for compatibility; no-op when adaptive off."""
        if not self.settings.adaptive_risk:
            return
        state.risk_scale = self._last_scale

    def evaluate(
        self,
        signal: Signal,
        state: SessionState,
        open_exposure_usd: float = 0.0,
        returns: pd.Series | None = None,
    ) -> RiskDecision:
        s = self.settings
        # Sizing equity is capped at the advertised budget (strategy_equity_usd).
        # The bot must never size against more than the owner authorized, even
        # when the real Kraken balance is larger. The real equity is still used
        # for "can I afford this at all" floors elsewhere; here it is a ceiling.
        real_equity = max(state.current_equity, 0.0)
        equity = min(real_equity, s.strategy_equity_usd) or real_equity
        # Exits (SELL) from an existing position are gated only by the signal
        # action and the engine's position/idempotency/gateway checks — NOT by
        # the entry breakers below. A trapped position must always be free to
        # exit regardless of cooldown, daily order cap, daily-loss, or drawdown.
        if signal.action is Action.SELL:
            return RiskDecision(True, "Exit signal approved")
        if signal.action is not Action.BUY:
            return RiskDecision(False, "No entry order requested")
        if state.realized_pnl_today <= -(equity * s.max_daily_loss_fraction):
            return RiskDecision(False, "Daily loss circuit breaker is active")
        if equity < s.min_order_notional_usd:
            return RiskDecision(False, f"Account equity {equity:.2f} below minimum order notional")
        drawdown = 1 - (equity / max(min(state.peak_equity, s.strategy_equity_usd), 0.01))
        if drawdown >= s.max_drawdown_fraction:
            return RiskDecision(False, "Maximum drawdown circuit breaker is active")
        if s.max_orders_per_day > 0 and state.orders_today >= s.max_orders_per_day:
            return RiskDecision(False, "Daily order limit reached")
        if state.last_order_at is not None:
            ready_at = state.last_order_at + timedelta(minutes=s.cooldown_minutes)
            if datetime.now(timezone.utc) < ready_at:
                return RiskDecision(False, "Trade cooldown is active")
        if signal.stop_price is None or signal.price <= signal.stop_price:
            return RiskDecision(False, "Invalid stop distance")

        exposure_cap = equity * (s.margin_exposure_fraction if s.margin_enabled else s.max_exposure_fraction)
        if open_exposure_usd >= exposure_cap:
            return RiskDecision(False, f"Exposure cap reached: {open_exposure_usd:.2f} >= {exposure_cap:.2f}")

        risk_budget = equity * self.effective_risk_per_trade(state)
        stop_fraction = (signal.price - signal.stop_price) / signal.price
        if stop_fraction <= 0:
            return RiskDecision(False, "Invalid stop distance")
        risk_sized_notional = risk_budget / stop_fraction

        # Vol-target + fractional-Kelly sizing (the durable edge). When we have
        # a returns series we let the sizer scale by live volatility; otherwise
        # we fall back to the static allocation cap so behavior is unchanged.
        stop_distance = signal.price - signal.stop_price
        if returns is not None and len(returns) > 0:
            sizing = self.sizer.size(
                equity=equity,
                current_price=signal.price,
                returns=returns,
                stop_distance=stop_distance,
            )
            sizer_notional = sizing.notional
        else:
            sizer_notional = equity * s.max_position_fraction

        # The sizer's output is one input; hard risk limits still bind.
        allocation_cap = equity * s.max_position_fraction
        notional = min(risk_sized_notional, sizer_notional, allocation_cap, equity)
        if open_exposure_usd + notional > exposure_cap:
            notional = max(0.0, exposure_cap - open_exposure_usd)
        if notional < s.min_order_notional_usd:
            return RiskDecision(False, "Calculated order is below minimum notional")
        planned_loss = notional * stop_fraction
        return RiskDecision(True, "Risk checks passed", round(notional, 2), round(planned_loss, 2))


"""Position sizing: volatility targeting + fractional Kelly overlay.

This is the highest-leverage risk component.  The bot's base risk limits
(``risk_per_trade``, ``max_position_fraction``, daily-loss / drawdown breakers)
remain hard ceilings; the ``PositionSizer`` only decides *how much* of that
allowance to deploy given live volatility and an estimate of edge.

Design (deliberately conservative):
  * Volatility targeting is the primary driver — size up when vol is low, down
    when vol spikes, so the position's contribution to portfolio vol stays near
    a target.  A volatility floor prevents explosive sizing in dead markets.
  * Fractional Kelly is a *conviction ceiling / multiplier*, never the sole
    driver.  It scales position size by estimated edge (win prob, payoff ratio,
    model confidence) but is capped well below full-Kelly.
  * The risk-per-trade percent is enforced as a hard stop-distance gate: if a
    stop is provided, notional can never risk more than ``risk_per_trade`` of
    equity.

Crypto note: we annualize realized volatility with 365 (24/7 markets), not 252.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd


@dataclass
class SizingConfig:
    """Sizer parameters, mapped from ``Settings`` so nothing is hardcoded."""

    target_vol: float = 0.12          # annualized target volatility
    kelly_fraction: float = 0.25      # 1/4 Kelly recommended
    max_risk_per_trade: float = 0.01  # 1% hard risk ceiling
    vol_lookbacks: tuple[int, int] = (10, 30)   # short + medium windows
    vol_weights: tuple[float, float] = (0.4, 0.6)
    min_vol: float = 0.05             # floor to avoid exploding size
    max_leverage: float = 2.0
    annualization: int = 365          # crypto trades 24/7


@dataclass
class EdgeEstimate:
    """Estimated edge used by the fractional-Kelly overlay."""

    win_prob: float = 0.5
    avg_win: float = 1.0
    avg_loss: float = 1.0
    confidence: float = 1.0   # model confidence 0..1

    def to_dict(self) -> dict[str, float]:
        return {
            "p": self.win_prob,
            "avg_win": self.avg_win,
            "avg_loss": self.avg_loss,
            "conf": self.confidence,
        }


@dataclass
class SizingResult:
    shares: float
    notional: float
    vol_used: float
    vol_scale: float
    kelly_mult: float
    leverage: float
    details: dict[str, Any] = field(default_factory=dict)


class PositionSizer:
    """Layered position sizing: vol-target -> fractional-Kelly -> risk-percent."""

    def __init__(self, cfg: SizingConfig | None = None) -> None:
        self.cfg = cfg or SizingConfig()

    # ── volatility ────────────────────────────────────────────

    def realized_vol(self, returns: pd.Series, annualize: bool = True) -> float:
        """Blended realized volatility across lookback windows, with a floor."""
        vols: list[float] = []
        for lb in self.cfg.vol_lookbacks:
            if len(returns) < lb:
                continue
            vol = float(returns.tail(lb).std())
            if annualize:
                vol *= np.sqrt(self.cfg.annualization)
            vols.append(float(vol))

        if not vols:
            return self.cfg.min_vol

        blended = float(np.average(vols, weights=self.cfg.vol_weights[: len(vols)]))
        return max(blended, self.cfg.min_vol)

    # ── fractional Kelly ──────────────────────────────────────

    def kelly_fractional(
        self,
        win_prob: float,
        avg_win: float,
        avg_loss: float,
        confidence: float = 1.0,
    ) -> float:
        """Fractional Kelly with confidence scaling and a hard ceiling."""
        if avg_loss <= 0 or win_prob <= 0 or win_prob >= 1:
            return 0.0

        payoff = avg_win / avg_loss
        q = 1 - win_prob
        full_kelly = (payoff * win_prob - q) / payoff
        full_kelly = max(0.0, full_kelly)
        frac = full_kelly * self.cfg.kelly_fraction * confidence
        # Hard ceiling even on fractional Kelly.
        return min(frac, 0.25)

    # ── sizing ────────────────────────────────────────────────

    def size(
        self,
        equity: float,
        current_price: float,
        returns: pd.Series,
        stop_distance: float | None = None,
        edge: EdgeEstimate | None = None,
        kelly_normalizer: float = 0.10,
    ) -> SizingResult:
        """Recommend a position sized by vol-targeting and (optionally) Kelly.

        ``kelly_normalizer`` maps the fractional-Kelly fraction onto a multiplier
        where ~10% Kelly ≈ 1.0x (tune per account).  The hard risk-per-trade
        gate still caps notional when a ``stop_distance`` is supplied.
        """
        cfg = self.cfg

        # 1. Volatility scale (primary driver).
        vol = self.realized_vol(returns)
        vol_scale = cfg.target_vol / vol
        vol_scale = min(vol_scale, cfg.max_leverage)

        # 2. Kelly / conviction overlay (multiplier only).
        kelly_mult = 1.0
        if edge is not None:
            k = self.kelly_fractional(
                edge.win_prob, edge.avg_win, edge.avg_loss, edge.confidence
            )
            kelly_mult = max(0.0, k / kelly_normalizer) if kelly_normalizer > 0 else 1.0

        # 3. Base notional from vol targeting * Kelly conviction.
        target_notional = equity * vol_scale * kelly_mult
        target_notional = min(target_notional, equity * cfg.max_leverage)

        shares = target_notional / current_price if current_price > 0 else 0.0

        # 4. Hard risk-per-trade gate (if a stop distance is known).
        if stop_distance is not None and stop_distance > 0:
            max_shares_risk = (equity * cfg.max_risk_per_trade) / stop_distance
            shares = min(shares, max_shares_risk)

        notional = shares * current_price
        return SizingResult(
            shares=shares,
            notional=notional,
            vol_used=vol,
            vol_scale=vol_scale,
            kelly_mult=kelly_mult,
            leverage=(notional / equity) if equity > 0 else 0.0,
            details={
                "target_vol": cfg.target_vol,
                "kelly_fraction": cfg.kelly_fraction,
                "max_risk_per_trade": cfg.max_risk_per_trade,
                "stop_distance": stop_distance,
                "edge": edge.to_dict() if edge else None,
            },
        )

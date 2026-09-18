"""Advanced order construction for the Kraken AddOrder API.

Kraken supports far more than a naked market order. This module builds the
parameter dictionaries the bot sends to ``AddOrder`` so the engine can use:

* **Limit (maker) entries** — post inside the spread to pay maker fees
  (0.16%) instead of taker fees (0.26%) and provide liquidity.
* **Bracket orders** — a single ``AddOrder`` carries a ``close`` block with a
  **stop-loss** and a **take-profit** (up to two conditional child orders).
  Kraken arms them when the parent fills and cancels whichever does not trigger
  first, so the exit is managed *by the exchange*, not by the bot polling.
* **Trailing stop** — the stop-loss can trail the running peak.

Everything here is pure construction: no network, no side effects. The gateway
validates precision and submits; the engine keeps it inside the idempotency,
precision, and risk gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Side = Literal["buy", "sell"]


def fmt_price(p: float, decimals: int | None = None) -> str:
    """Format a price the way Kraken accepts for a given pair precision.

    Kraken rejects over-precise prices per pair — BTC/USD allows 1 decimal
    ("price can only be specified up to 1 decimals"). ``fmt_price`` used to
    emit up to 8 decimals, so a limit entry on a 1-decimal pair was always
    rejected. Pass the pair's ``pair_decimals`` to round to what the exchange
    actually accepts. ``decimals=None`` keeps the old plain-trim behaviour.
    """
    if decimals is None:
        s = f"{p:.8f}".rstrip("0").rstrip(".")
        return s if s else "0"
    decimals = max(0, int(decimals))
    s = f"{p:.{decimals}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s if s else "0"


def limit_entry_price(side: str, touch: float, offset_pct: float,
                      decimals: int | None = None) -> float:
    """Price to post a limit order inside the spread.

    Buy: slightly below the ask (better price, still likely to fill).
    Sell: slightly above the bid. ``offset_pct`` is the fraction of ``touch``
    used as the improvement (e.g. 0.001 = 0.1%).

    When ``decimals`` is given the result is rounded to that precision so the
    posted price survives Kraken's per-pair decimal check. The rounded price
    must stay strictly inside the spread, otherwise the limit degenerates into
    a market order and pays the taker fee anyway.
    """
    if side == "buy":
        raw = touch * (1.0 - offset_pct)
    else:
        raw = touch * (1.0 + offset_pct)
    if decimals is None:
        return raw
    return round(raw, max(0, int(decimals)))


@dataclass
class BracketPlan:
    """Resolved entry + exit plan for one trade, ready for the gateway.

    The protective stop is attached as a Kraken ``close`` conditional order on
    the parent ``AddOrder``. Kraken reliably accepts a SINGLE conditional close
    leg (stop-loss OR take-profit). The take-profit, when used, is managed as a
    separate resting limit order by the engine so we never rely on the
    two-leg ``close[1]`` form (which this account's tier rejects with
    ``EAPI:Bad request``). ``userref`` is the parent's idempotency key;
    children get deterministic derived refs.
    """

    pair: str
    side: Side
    volume: str
    ordertype: str = "market"          # "market" | "limit"
    entry_price: str | None = None     # required when ordertype == "limit"
    stop_loss: float | None = None
    take_profit: float | None = None
    userref: int | None = None
    trailing: bool = False
    pair_decimals: int = 8             # Kraken price precision for the pair

    def _r(self, price: float) -> str:
        """Round a price to the pair's decimal precision (Kraken requirement)."""
        from decimal import Decimal, ROUND_DOWN
        q = Decimal(1).scaleb(-self.pair_decimals)
        return str(Decimal(str(price)).quantize(q, rounding=ROUND_DOWN))

    def to_addorder_params(self) -> dict:
        """Flatten into Kraken ``AddOrder`` parameters (no auth/nonce)."""
        params: dict[str, str] = {
            "pair": self.pair,
            "type": self.side,
            "ordertype": self.ordertype,
            "volume": self.volume,
        }
        if self.ordertype == "limit" and self.entry_price is not None:
            params["price"] = self.entry_price
        if self.userref is not None:
            params["userref"] = str(self.userref)

        # Protective stop: a single conditional close leg (stop-loss). This is
        # the reliability-proven form; the two-leg close[1] OCO is avoided.
        if self.stop_loss is not None:
            base = self.userref or 0
            params["close[ordertype]"] = "stop-loss"
            if self.trailing:
                params["close[trailing]"] = _trailing_offset(self)
            else:
                params["close[price]"] = self._r(self.stop_loss)
            params["close[userref]"] = str(base + 1)
        # Take-profit (if set) is intentionally NOT attached here — the engine
        # places it as a separate resting limit order to sidestep close[1].
        return params


def build_take_profit_order(pair: str, side: str, volume: str, tp_price: float,
                            userref: int | None = None, pair_decimals: int = 8) -> dict:
    """Params for a standalone take-profit limit order (opposite side of entry)."""
    from decimal import Decimal, ROUND_DOWN
    q = Decimal(1).scaleb(-pair_decimals)
    tp = str(Decimal(str(tp_price)).quantize(q, rounding=ROUND_DOWN))
    opposite = "sell" if side == "buy" else "buy"
    params = {
        "pair": pair, "type": opposite, "ordertype": "limit",
        "price": tp, "volume": volume,
    }
    if userref is not None:
        params["userref"] = str(userref)
    return params


def _trailing_offset(plan: BracketPlan) -> str:
    """Trailing stop offset as a percentage string Kraken accepts."""
    ref = float(plan.entry_price) if plan.entry_price else 0.0
    if ref <= 0:
        return "4%"
    sl = plan.stop_loss or ref
    pct = abs(ref - sl) / ref * 100.0
    return f"{max(1, round(pct))}%"


def bracket_prices(
    side: str, entry: float, stop_loss_pct: float, take_profit_pct: float
) -> tuple[float, float]:
    """Compute absolute stop-loss and take-profit prices for a parent side."""
    if side == "buy":  # long
        sl = entry * (1.0 - stop_loss_pct)
        tp = entry * (1.0 + take_profit_pct)
    else:  # short
        sl = entry * (1.0 + stop_loss_pct)
        tp = entry * (1.0 - take_profit_pct)
    return sl, tp

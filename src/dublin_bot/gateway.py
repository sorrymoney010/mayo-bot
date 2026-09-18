"""Shared broker gateway interface.

Every broker adapter (Alpaca, Kraken, …) implements this protocol so the engine
and dashboard stay broker-agnostic.  Live order submission is gated inside each
adapter behind the dry_run / paper_trading / allow_live_trading flags.

Read-only methods are safe to call in any mode.  Methods in ``ExtendedGateway``
are optional capabilities: the engine feature-detects them with ``hasattr`` and
degrades gracefully when an adapter does not provide them.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class BrokerGateway(Protocol):
    """Minimal interface every broker adapter must implement."""

    # ── Market data ──────────────────────────────────────
    def get_bars(self) -> "object":  # returns pd.DataFrame
        ...

    # ── Account / positions ──────────────────────────────
    def account_equity(self) -> float: ...
    def has_position(self) -> bool: ...
    def positions(self) -> list[dict]: ...
    def orders(self) -> list[dict]: ...

    # ── Order execution (gated inside the adapter) ───────
    def buy_notional(self, notional_usd: float, *, userref: int | None = None) -> str: ...
    def close_position(self, *, userref: int | None = None) -> str: ...


@runtime_checkable
class ExtendedGateway(BrokerGateway, Protocol):
    """Optional capabilities used by the safety pipeline when present."""

    def check_freshness(self, bars: "object" = None) -> "object": ...
    def market_quality(self) -> dict: ...
    def health(self) -> dict: ...
    def find_order_by_userref(self, userref: int) -> dict | None: ...

"""Extra live locks for margin and futures order submission.

These are independent of ``PAPER_TRADING`` / ``DRY_RUN`` / ``ALLOW_LIVE_TRADING``
and default OFF. Opening one of them is not enough: every existing lock must
also be opened (paper off, dry-run off, live allowed, acknowledgement set)
or the call refuses before any transport runs.

Paper sleeves never call these functions. They simulate fills locally and
read only public market data.
"""
from __future__ import annotations

from typing import Any, Callable

# Kraken derivatives private order route. Present only in this module so a
# paper import of the public client cannot reach it by accident.
FUTURES_SENDORDER_PATH = "/derivatives/api/v3/sendorder"
# Kraken spot margin is still AddOrder plus leverage params. The guard rejects
# that combination unless the margin lock and the existing locks are all open.
SPOT_ADDORDER_PATH = "/0/private/AddOrder"


class VenueLockError(RuntimeError):
    """A margin or futures live order was refused by a lock."""


def _existing_locks_open(settings) -> tuple[bool, str]:
    if getattr(settings, "paper_trading", True):
        return False, "PAPER_TRADING is on"
    if getattr(settings, "dry_run", True):
        return False, "DRY_RUN is on"
    if not getattr(settings, "allow_live_trading", False):
        return False, "ALLOW_LIVE_TRADING is off"
    ack = getattr(settings, "live_risk_acknowledgement", "")
    if ack != "I_ACCEPT_LIVE_TRADING_RISK":
        return False, "live-risk acknowledgement is missing"
    return True, "existing locks open"


def live_futures_submission_allowed(settings) -> tuple[bool, str]:
    if not getattr(settings, "allow_futures_live_orders", False):
        return False, "ALLOW_FUTURES_LIVE_ORDERS is off"
    ok, why = _existing_locks_open(settings)
    if not ok:
        return False, f"futures live orders refused: {why}"
    return True, "futures live orders allowed"


def live_margin_submission_allowed(settings) -> tuple[bool, str]:
    if not getattr(settings, "allow_margin_live_orders", False):
        return False, "ALLOW_MARGIN_LIVE_ORDERS is off"
    ok, why = _existing_locks_open(settings)
    if not ok:
        return False, f"margin live orders refused: {why}"
    return True, "margin live orders allowed"


def assert_live_futures_submission(settings) -> None:
    ok, why = live_futures_submission_allowed(settings)
    if not ok:
        raise VenueLockError(why)


def assert_live_margin_submission(settings) -> None:
    ok, why = live_margin_submission_allowed(settings)
    if not ok:
        raise VenueLockError(why)


def submit_futures_order(settings, payload: dict, transport: Callable[[str, dict], Any]):
    """Submit a futures order. Refuses before ``transport`` unless every lock is open."""
    assert_live_futures_submission(settings)
    return transport(FUTURES_SENDORDER_PATH, payload)


def submit_margin_order(settings, payload: dict, transport: Callable[[str, dict], Any]):
    """Submit a spot-margin order. Refuses before ``transport`` unless every lock is open."""
    assert_live_margin_submission(settings)
    if not payload.get("leverage") and payload.get("ordertype") != "margin":
        # Still a live margin path: the caller asked for this function.
        # Require an explicit leverage so a plain spot order cannot be
        # smuggled through the margin entry point.
        raise VenueLockError("margin live order missing leverage")
    return transport(SPOT_ADDORDER_PATH, payload)

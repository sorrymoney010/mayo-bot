"""Typed exception hierarchy for broker interaction.

Kraken returns errors as a list of strings in the JSON body (``{"error": [...]}``)
*and* as HTTP status codes.  Callers need to distinguish between:

* transient failures worth retrying (rate limit, service busy, network)
* permanent failures that must halt the cycle (bad credentials, bad pair)
* safety failures that must never be retried (stale data, precision violation)

Retrying a permanent auth error just burns rate-limit budget; retrying nothing
at all makes the bot fragile.  The classification lives in ``classify_kraken_error``.
"""

from __future__ import annotations


class DublinError(Exception):
    """Base class for every Dublin-raised error."""


class BrokerError(DublinError):
    """Any failure originating from the exchange or its transport."""

    retryable: bool = False


class TransientBrokerError(BrokerError):
    """Temporary failure — safe to retry with backoff."""

    retryable = True


class RateLimitError(TransientBrokerError):
    """Exchange rate limit hit (HTTP 429 or EAPI:Rate limit exceeded)."""


class AuthenticationError(BrokerError):
    """Invalid key, invalid signature, or invalid nonce. Never retried blindly."""


class InvalidRequestError(BrokerError):
    """Malformed request: unknown pair, bad argument, unsupported option."""


class InsufficientFundsError(BrokerError):
    """Not enough balance to place the order."""


class StaleDataError(DublinError):
    """Market data is too old to trade on. Always fatal for the current cycle."""


class PrecisionError(DublinError):
    """Order violates the pair's precision or minimum-size constraints."""


class DuplicateOrderError(DublinError):
    """An order with this idempotency key was already submitted."""


class SafetyLockError(DublinError):
    """An operation was attempted while the safety locks forbid it."""


# Kraken error-string prefixes → exception class.
# Reference: https://docs.kraken.com/api/docs/rest-api/get-server-time (error codes appendix)
_ERROR_MAP: tuple[tuple[str, type[BrokerError]], ...] = (
    ("EAPI:Rate limit exceeded", RateLimitError),
    ("EGeneral:Too many requests", RateLimitError),
    ("EOrder:Rate limit exceeded", RateLimitError),
    ("EAPI:Invalid nonce", AuthenticationError),
    ("EAPI:Invalid key", AuthenticationError),
    ("EAPI:Invalid signature", AuthenticationError),
    ("EGeneral:Permission denied", AuthenticationError),
    ("EAPI:Bad request", InvalidRequestError),
    ("EGeneral:Invalid arguments", InvalidRequestError),
    ("EQuery:Unknown asset pair", InvalidRequestError),
    ("EQuery:Unknown asset", InvalidRequestError),
    ("EOrder:Insufficient funds", InsufficientFundsError),
    ("EOrder:Insufficient margin", InsufficientFundsError),
    ("EService:Unavailable", TransientBrokerError),
    ("EService:Busy", TransientBrokerError),
    ("EService:Market in cancel_only", TransientBrokerError),
    ("EService:Market in post_only", TransientBrokerError),
)


def classify_kraken_error(errors: list[str]) -> BrokerError:
    """Map a Kraken ``error`` array onto the most specific exception we know.

    Unknown codes fall back to a non-retryable ``BrokerError`` — failing closed
    is the correct default for a trading system.
    """
    joined = "; ".join(errors) if errors else "unknown Kraken error"
    for prefix, exc_type in _ERROR_MAP:
        for code in errors:
            if code.startswith(prefix):
                return exc_type(joined)
    return BrokerError(joined)

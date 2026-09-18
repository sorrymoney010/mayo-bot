"""Client-side rate limiting for Kraken REST.

Kraken enforces a *counter* model rather than requests-per-second: every private
call adds to a per-key counter that decays over time, and exceeding the ceiling
returns ``EAPI:Rate limit exceeded``.  Public endpoints are limited separately
and more loosely (roughly 1 request/second sustained).

We implement a token bucket per endpoint class.  It is deliberately more
conservative than Kraken's published limits — being throttled mid-cycle in a
trading system is far more costly than being a few hundred milliseconds slower.

Tier defaults follow the Starter tier (max counter 15, decay 0.33/s), which is
the safe assumption when the account tier is unknown.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitTier:
    """Kraken counter parameters for an account tier."""

    max_counter: float
    decay_per_second: float

    @classmethod
    def starter(cls) -> "RateLimitTier":
        return cls(max_counter=15.0, decay_per_second=0.33)

    @classmethod
    def intermediate(cls) -> "RateLimitTier":
        return cls(max_counter=20.0, decay_per_second=0.5)

    @classmethod
    def pro(cls) -> "RateLimitTier":
        return cls(max_counter=20.0, decay_per_second=1.0)


# Approximate counter cost per private endpoint (Kraken publishes these).
_PRIVATE_COST: dict[str, float] = {
    "Balance": 1.0,
    "TradeBalance": 2.0,
    "OpenOrders": 1.0,
    "ClosedOrders": 2.0,
    "QueryOrders": 1.0,
    "OpenPositions": 1.0,
    "TradesHistory": 2.0,
    "AddOrder": 0.0,   # order endpoints use the separate order-rate counter
    "CancelOrder": 0.0,
}


class TokenBucket:
    """Simple thread-safe token bucket with a monotonic clock."""

    def __init__(self, capacity: float, refill_per_second: float,
                 time_fn=time.monotonic, sleep_fn=time.sleep) -> None:
        if capacity <= 0 or refill_per_second <= 0:
            raise ValueError("capacity and refill_per_second must be positive")
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self._tokens = capacity
        self._updated = time_fn()
        self._time = time_fn
        self._sleep = sleep_fn
        self._lock = threading.Lock()

    def _refill_locked(self) -> None:
        now = self._time()
        elapsed = max(0.0, now - self._updated)
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_second)
        self._updated = now

    @property
    def tokens(self) -> float:
        with self._lock:
            self._refill_locked()
            return self._tokens

    def try_acquire(self, cost: float = 1.0) -> bool:
        """Take ``cost`` tokens without blocking. False if the budget is short."""
        with self._lock:
            self._refill_locked()
            if self._tokens >= cost:
                self._tokens -= cost
                return True
            return False

    def acquire(self, cost: float = 1.0, timeout: float = 30.0) -> float:
        """Block until ``cost`` tokens are available. Returns seconds waited.

        Raises ``TimeoutError`` rather than waiting forever, so a wedged bucket
        surfaces as a cycle error instead of a hung process.
        """
        if cost > self.capacity:
            raise ValueError(f"cost {cost} exceeds bucket capacity {self.capacity}")
        waited = 0.0
        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens >= cost:
                    self._tokens -= cost
                    return waited
                deficit = cost - self._tokens
                delay = deficit / self.refill_per_second
            if waited + delay > timeout:
                raise TimeoutError(
                    f"rate limiter timeout: needed {cost} tokens, waited {waited:.1f}s"
                )
            self._sleep(delay)
            waited += delay


class KrakenRateLimiter:
    """Separate buckets for public and private Kraken endpoints."""

    def __init__(self, tier: RateLimitTier | None = None,
                 time_fn=time.monotonic, sleep_fn=time.sleep) -> None:
        tier = tier or RateLimitTier.starter()
        self.tier = tier
        # Public: ~1 req/s sustained, small burst allowance.
        self.public = TokenBucket(5.0, 1.0, time_fn, sleep_fn)
        self.private = TokenBucket(tier.max_counter, tier.decay_per_second, time_fn, sleep_fn)

    @staticmethod
    def private_cost(method: str) -> float:
        return _PRIVATE_COST.get(method, 2.0)

    def acquire_public(self, timeout: float = 30.0) -> float:
        return self.public.acquire(1.0, timeout)

    def acquire_private(self, method: str, timeout: float = 30.0) -> float:
        cost = max(self.private_cost(method), 1.0)
        return self.private.acquire(cost, timeout)

    def snapshot(self) -> dict[str, float]:
        """Observability hook for the dashboard."""
        return {
            "public_tokens": round(self.public.tokens, 2),
            "public_capacity": self.public.capacity,
            "private_tokens": round(self.private.tokens, 2),
            "private_capacity": self.private.capacity,
        }

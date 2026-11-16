"""Wall-clock waits that stay correct across system sleep and hibernation."""
from __future__ import annotations

import time
from collections.abc import Callable


def sleep_until(
    target: float,
    *,
    now_fn: Callable[[], float] = time.time,
    sleep_fn: Callable[[float], None] = time.sleep,
    max_slice: float = 30.0,
) -> None:
    """Block until wall-clock ``target`` (unix time), in slices of at most ``max_slice``.

    A single long ``time.sleep(delay)`` can keep sleeping for the remaining delay
    after the box wakes, even when wall-clock time has already passed the target.
    Re-checking ``time.time()`` after every short slice means a cycle starts as
    soon as the target has passed. If wall-clock time is already at or past
    ``target``, return immediately.
    """
    if max_slice <= 0:
        raise ValueError("max_slice must be positive")
    while True:
        remaining = target - now_fn()
        if remaining <= 0:
            return
        sleep_fn(remaining if remaining < max_slice else max_slice)

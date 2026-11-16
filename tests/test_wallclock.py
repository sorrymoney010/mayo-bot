"""sleep_until: short wall-clock slices, no leftover delay after a hibernation overshoot."""
from __future__ import annotations

import pytest

from dublin_bot.wallclock import sleep_until


def test_sleep_until_returns_immediately_when_target_already_passed():
    sleeps: list[float] = []
    sleep_until(900.0, now_fn=lambda: 1000.0, sleep_fn=sleeps.append, max_slice=30.0)
    assert sleeps == []


def test_sleep_until_uses_short_slices_until_target():
    clock = {"t": 0.0}
    sleeps: list[float] = []

    def _sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["t"] += seconds

    sleep_until(75.0, now_fn=lambda: clock["t"], sleep_fn=_sleep, max_slice=30.0)
    assert sleeps == [30.0, 30.0, 15.0]
    assert clock["t"] == 75.0


def test_sleep_until_stops_after_hibernation_overshoots_target():
    """A long sleep leftover would stall here; sliced waits must return after the jump."""
    clock = {"t": 1_000.0}
    target = 1_000.0 + 3_600.0
    sleeps: list[float] = []

    def _sleep(seconds: float) -> None:
        sleeps.append(seconds)
        # First slice: box hibernates ~9.5 hours instead of sleeping 30s.
        clock["t"] += 9.5 * 3600 if len(sleeps) == 1 else seconds

    sleep_until(target, now_fn=lambda: clock["t"], sleep_fn=_sleep, max_slice=30.0)
    assert sleeps == [30.0]
    assert clock["t"] >= target


def test_sleep_until_rejects_non_positive_slice():
    with pytest.raises(ValueError):
        sleep_until(1.0, now_fn=lambda: 0.0, sleep_fn=lambda _s: None, max_slice=0.0)

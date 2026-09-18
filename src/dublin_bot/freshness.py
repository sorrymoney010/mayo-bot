"""Market-data freshness validation.

This module exists because of a real incident recorded in the project handoff:
the Alpaca feed was serving bars from July 14 while the wall clock read
August 4, and nothing in the pipeline noticed.  A strategy fed three-week-old
candles will happily produce confident, catastrophic signals.

Two independent checks are performed:

1. **Bar age** — how long ago the most recent candle closed, relative to the
   bar interval.  A 60-minute feed may legitimately be up to ~60 minutes behind;
   three hours behind is a fault.
2. **Clock skew** — the difference between exchange server time and local time.
   Kraken rejects private requests when local clocks drift far enough, and skew
   also corrupts the bar-age calculation itself.

Both return a structured verdict so the dashboard can display *why* trading is
blocked rather than merely that it is.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone

import pandas as pd

from .errors import StaleDataError


@dataclass(frozen=True)
class FreshnessVerdict:
    """Outcome of a data-freshness evaluation."""

    fresh: bool
    reason: str
    bar_age_minutes: float | None = None
    max_age_minutes: float | None = None
    clock_skew_seconds: float | None = None
    last_bar_timestamp: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def raise_if_stale(self) -> None:
        if not self.fresh:
            raise StaleDataError(self.reason)


def _as_utc(value: object) -> datetime | None:
    """Coerce a pandas/py timestamp into a tz-aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class FreshnessGuard:
    """Validates that market data is recent enough to trade on."""

    def __init__(
        self,
        timeframe_minutes: int,
        *,
        max_bar_age_multiple: float = 3.0,
        max_clock_skew_seconds: float = 30.0,
    ) -> None:
        if timeframe_minutes <= 0:
            raise ValueError("timeframe_minutes must be positive")
        self.timeframe_minutes = timeframe_minutes
        self.max_bar_age_multiple = max_bar_age_multiple
        self.max_clock_skew_seconds = max_clock_skew_seconds

    @property
    def max_bar_age_minutes(self) -> float:
        """Tolerated age of the newest bar.

        One interval is expected (the current bar has not closed yet), so the
        allowance is ``interval × multiple`` with the multiple defaulting to 3 —
        enough to absorb a missed poll without tolerating a dead feed.
        """
        return self.timeframe_minutes * self.max_bar_age_multiple

    def evaluate_bars(
        self,
        bars: pd.DataFrame,
        *,
        now: datetime | None = None,
        server_time: float | None = None,
    ) -> FreshnessVerdict:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

        skew: float | None = None
        if server_time is not None:
            skew = server_time - now.timestamp()
            if abs(skew) > self.max_clock_skew_seconds:
                return FreshnessVerdict(
                    fresh=False,
                    reason=(
                        f"Clock skew {skew:.1f}s exceeds "
                        f"{self.max_clock_skew_seconds:.0f}s tolerance"
                    ),
                    clock_skew_seconds=round(skew, 3),
                )

        if bars is None or len(bars) == 0:
            return FreshnessVerdict(False, "No bars returned by the feed",
                                    clock_skew_seconds=skew)

        last_ts = _as_utc(bars.index[-1])
        if last_ts is None:
            return FreshnessVerdict(False, "Bar index is not a usable timestamp",
                                    clock_skew_seconds=skew)

        age_minutes = (now - last_ts).total_seconds() / 60.0
        limit = self.max_bar_age_minutes

        if age_minutes < -self.timeframe_minutes:
            # A bar dated meaningfully in the future means the feed or the local
            # clock is wrong; either way it is not safe to trade.
            return FreshnessVerdict(
                fresh=False,
                reason=f"Newest bar is {abs(age_minutes):.1f} min in the future",
                bar_age_minutes=round(age_minutes, 2),
                max_age_minutes=limit,
                clock_skew_seconds=skew,
                last_bar_timestamp=last_ts.isoformat(),
            )

        if age_minutes > limit:
            return FreshnessVerdict(
                fresh=False,
                reason=(
                    f"Market data is stale: newest bar is {age_minutes:.1f} min old "
                    f"(limit {limit:.0f} min)"
                ),
                bar_age_minutes=round(age_minutes, 2),
                max_age_minutes=limit,
                clock_skew_seconds=skew,
                last_bar_timestamp=last_ts.isoformat(),
            )

        return FreshnessVerdict(
            fresh=True,
            reason="Market data is fresh",
            bar_age_minutes=round(age_minutes, 2),
            max_age_minutes=limit,
            clock_skew_seconds=round(skew, 3) if skew is not None else None,
            last_bar_timestamp=last_ts.isoformat(),
        )


def check_monotonic_bars(bars: pd.DataFrame) -> None:
    """Guard against duplicated or out-of-order candles.

    Repeated timestamps silently corrupt every rolling indicator, so this is
    treated as a data fault rather than something to clean up quietly.
    """
    if bars is None or len(bars) < 2:
        return
    index = bars.index
    if not index.is_monotonic_increasing:
        raise StaleDataError("Bar timestamps are not monotonically increasing")
    if index.has_duplicates:
        raise StaleDataError("Bar index contains duplicate timestamps")

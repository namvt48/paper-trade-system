"""Publish-window helpers for signal scheduling.

Used by engines with ``publish_at_midnight_utc`` enabled in spec.json:
signals may only be published on the scan whose candle closes exactly at a
00:00 UTC day boundary, so entries happen at ~00:00 (plus compute time)
instead of at arbitrary hours.
"""
from __future__ import annotations

DAY_MS = 86_400_000


def is_midnight_close_utc(candle_open_ms: int, tf_ms: int) -> bool:
    """True when the candle closes exactly on a 00:00 UTC day boundary."""
    return (candle_open_ms + tf_ms) % DAY_MS == 0


def is_close_aligned_rebalance(
    candle_open_ms: int, tf_ms: int, rebalance_bars: int
) -> bool:
    """Rebalance cadence counted on candle-close boundaries.

    The default schedule counts on candle-open boundaries (``bar_number %
    rebalance_bars``), which publishes one bar after the day boundary. When
    gating publishes to midnight closes, the cadence must count the closing
    bar instead so the two conditions can coincide.
    """
    return ((candle_open_ms + tf_ms) // tf_ms) % rebalance_bars == 0


def is_rebalance_due(
    candle_open_ms: int,
    tf_ms: int,
    rebalance_bars: int,
    *,
    publish_at_midnight_utc: bool = False,
    rebalance_on_close: bool = False,
) -> bool:
    """Return whether this completed candle is a configured rebalance point.

    ``rebalance_on_close`` keeps the cadence anchored to candle-close time
    without restricting it to midnight. This is required for periods such as
    36 hours: combining a 36-hour cadence with a midnight-only gate otherwise
    produces a 72-hour effective cadence.
    """
    if publish_at_midnight_utc and not is_midnight_close_utc(
        candle_open_ms, tf_ms
    ):
        return False
    if publish_at_midnight_utc or rebalance_on_close:
        return is_close_aligned_rebalance(
            candle_open_ms, tf_ms, rebalance_bars
        )
    return (candle_open_ms // tf_ms) % rebalance_bars == 0

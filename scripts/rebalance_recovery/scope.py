"""Frozen scope and schedule calculation for the 2026-07-16 recovery incident."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, time, timedelta, timezone
from typing import Final

from .domain import (
    AlphaId,
    AlphaSchedule,
    CandleOpenMs,
    RecoveryPoint,
)

EXCLUDED_ALPHA: Final = AlphaId("1d-trend60cmf")
OFFSET_SECONDS: Final = 5
TIMEFRAME_MS: Final = {
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

INCIDENT_SCHEDULES: Final = (
    AlphaSchedule(AlphaId("1d-kertrend"), "1d", 1),
    AlphaSchedule(AlphaId("1d-trend60cmf"), "1d", 1, "2026-07-31"),
    AlphaSchedule(AlphaId("1d-vwaprev"), "1d", 1),
    AlphaSchedule(AlphaId("1d-iamp"), "1d", 1),
    AlphaSchedule(AlphaId("1d-chmom"), "1d", 1, "2026-07-17"),
    AlphaSchedule(AlphaId("ensemble-1d"), "1d", 1, "2026-07-17"),
    AlphaSchedule(AlphaId("15m-blend-close"), "15m", 96),
    AlphaSchedule(AlphaId("15m-blend-close-2-v3"), "15m", 192),
    AlphaSchedule(AlphaId("15m-blend-close-36h"), "15m", 144),
    AlphaSchedule(AlphaId("15m-blend-close-b"), "15m", 96),
    AlphaSchedule(AlphaId("15m-blend-close-b-36h"), "15m", 144),
    AlphaSchedule(AlphaId("15m-breakout"), "15m", 192),
    AlphaSchedule(AlphaId("1h-blend-close"), "1h", 24),
    AlphaSchedule(AlphaId("1h-blend-close-c"), "1h", 24),
    AlphaSchedule(AlphaId("1h-blend-close-36h"), "1h", 36),
    AlphaSchedule(AlphaId("1h-decay-close"), "1h", 24),
    AlphaSchedule(AlphaId("1h-decay-close-36h"), "1h", 36),
    AlphaSchedule(AlphaId("1h-decay-close-v3"), "1h", 48),
    AlphaSchedule(AlphaId("1h-decay-vwap-36h"), "1h", 36),
    AlphaSchedule(AlphaId("1h-trend-breakout"), "1h", 48),
    AlphaSchedule(AlphaId("1h-trend-skew"), "1h", 48),
    AlphaSchedule(AlphaId("4h-amihud"), "4h", 12),
    AlphaSchedule(AlphaId("4h-trend-close-v3"), "4h", 12),
    AlphaSchedule(AlphaId("4h-trend-z"), "4h", 12),
    AlphaSchedule(AlphaId("songthanv11"), "15m", 96),
    AlphaSchedule(AlphaId("songthanv8"), "1h", 24),
    AlphaSchedule(AlphaId("15m-trend-close-36h-reverse"), "15m", 144),
    AlphaSchedule(AlphaId("15m-trend-vwap-36h-reverse"), "15m", 144),
)


def build_incident_points(
    start: date,
    end: date,
    only_alphas: Iterable[str] | None = None,
) -> tuple[RecoveryPoint, ...]:
    """Return due rebalance points, respecting cadence and activation boundaries.

    When ``only_alphas`` is provided, restrict the schedule to that allowlist so a
    recovery run never fabricates cycles for alphas that rebalanced correctly.
    ``None`` preserves the full frozen incident schedule (16-17 reproducibility).
    """
    allowlist = None if only_alphas is None else {str(a) for a in only_alphas}
    if allowlist is not None:
        known = {str(schedule.alpha_id) for schedule in INCIDENT_SCHEDULES}
        unknown = allowlist - known
        if unknown:
            raise ValueError(f"only_alphas not in incident schedule: {sorted(unknown)}")
    points: list[RecoveryPoint] = []
    day = start
    while day <= end:
        close_at = datetime.combine(day, time.min, tzinfo=timezone.utc)
        for schedule in INCIDENT_SCHEDULES:
            if allowlist is not None and str(schedule.alpha_id) not in allowlist:
                continue
            if _is_active(schedule, day) and _is_due(schedule, close_at):
                timeframe_ms = TIMEFRAME_MS[schedule.timeframe]
                candle_open_ms = int(close_at.timestamp() * 1000) - timeframe_ms
                points.append(
                    RecoveryPoint(
                        alpha_id=schedule.alpha_id,
                        timeframe=schedule.timeframe,
                        candle_open_ms=CandleOpenMs(candle_open_ms),
                        event_at=close_at + timedelta(seconds=OFFSET_SECONDS),
                    )
                )
        day += timedelta(days=1)
    return tuple(sorted(points, key=lambda point: (point.event_at, point.alpha_id)))


def build_close_points(
    close_at: datetime,
    only_alphas: Iterable[str],
) -> tuple[RecoveryPoint, ...]:
    """Build guarded recovery points for one explicit completed-candle close.

    This path is for a known missed live cycle outside the frozen 16-17 July
    incident. Every requested alpha must be known, active, and due at the
    supplied UTC close; otherwise recovery stops instead of fabricating rows.
    """
    if close_at.tzinfo is None:
        raise ValueError("close_at must be timezone-aware")
    close_at = close_at.astimezone(timezone.utc)
    if close_at.second or close_at.microsecond:
        raise ValueError("close_at must be an exact candle-close minute")

    allowlist = {str(alpha_id) for alpha_id in only_alphas}
    if not allowlist:
        raise ValueError("explicit close recovery requires at least one alpha")
    schedules = {str(schedule.alpha_id): schedule for schedule in INCIDENT_SCHEDULES}
    unknown = allowlist - schedules.keys()
    if unknown:
        raise ValueError(f"only_alphas not in incident schedule: {sorted(unknown)}")

    points: list[RecoveryPoint] = []
    not_due: list[str] = []
    for alpha_id in sorted(allowlist):
        schedule = schedules[alpha_id]
        if not _is_active(schedule, close_at.date()) or not _is_due(schedule, close_at):
            not_due.append(alpha_id)
            continue
        timeframe_ms = TIMEFRAME_MS[schedule.timeframe]
        points.append(
            RecoveryPoint(
                alpha_id=schedule.alpha_id,
                timeframe=schedule.timeframe,
                candle_open_ms=CandleOpenMs(
                    int(close_at.timestamp() * 1000) - timeframe_ms
                ),
                event_at=close_at + timedelta(seconds=OFFSET_SECONDS),
            )
        )
    if not_due:
        raise ValueError(f"alphas are not due at {close_at.isoformat()}: {not_due}")
    return tuple(sorted(points, key=lambda point: point.alpha_id))


def _is_active(schedule: AlphaSchedule, close_day: date) -> bool:
    """Apply explicit deployment boundaries for alphas created during the incident."""
    if schedule.activation_close_date is None:
        return True
    return close_day >= date.fromisoformat(schedule.activation_close_date)


def _is_due(schedule: AlphaSchedule, close_at: datetime) -> bool:
    """Mirror the runner's close-aligned midnight cadence calculation."""
    timeframe_ms = TIMEFRAME_MS[schedule.timeframe]
    close_ms = int(close_at.timestamp() * 1000)
    if schedule.timeframe == "1d":
        candle_open_ms = close_ms - timeframe_ms
        return (candle_open_ms // timeframe_ms) % schedule.rebalance_bars == 0
    return (close_ms // timeframe_ms) % schedule.rebalance_bars == 0

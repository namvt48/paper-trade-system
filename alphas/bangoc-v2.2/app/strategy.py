"""Indicator calculations and H1-dot admission rule for bangoc-v2.2."""

from dataclasses import dataclass
from math import ceil, floor
from typing import Literal

from .config import settings


BangocSide = Literal["LONG", "SHORT"]


@dataclass(frozen=True)
class BangocIndicators:
    """Contains the M15 dot, zone, and resulting trade side for one candle."""

    side: BangocSide | None
    close: float
    indi1_green: bool
    indi1_acol: float
    indi1_acol_prev: float
    indi2_green: bool
    indi2_poc: float
    indi2_lower: float
    indi2_upper: float


def get_candle_seconds(tf: str) -> int:
    if tf.endswith("m"):
        return int(tf[:-1]) * 60
    if tf.endswith("h"):
        return int(tf[:-1]) * 3600
    return 3600


def _sma_series(vals: list[float], length: int) -> list[float | None]:
    out: list[float | None] = [None] * len(vals)
    total = 0.0
    for idx, value in enumerate(vals):
        total += value
        if idx >= length:
            total -= vals[idx - length]
        if idx >= length - 1:
            out[idx] = total / length
    return out


def _percentile_linear(vals: list[float], percentile: float) -> float:
    if not vals:
        raise ValueError("percentile requires at least one value")
    if len(vals) == 1:
        return vals[0]

    ordered = sorted(vals)
    pct = min(100.0, max(0.0, percentile))
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = floor(rank)
    hi = ceil(rank)
    if lo == hi:
        return ordered[lo]
    weight = rank - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * weight


def _median(vals: list[float]) -> float:
    ordered = sorted(vals)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _compute_indi1(
    close_list: list[float],
    sma_len: int,
    norm_window: int,
    threshold: float,
) -> tuple[bool | None, float | None, float | None]:
    min_bars = sma_len + norm_window + 5
    if len(close_list) < min_bars:
        return None, None, None

    avg = _sma_series(close_list, sma_len)
    avg_diff: list[float | None] = [None] * len(close_list)
    for idx in range(5, len(close_list)):
        current_avg = avg[idx]
        previous_avg = avg[idx - 5]
        if current_avg is not None and previous_avg is not None:
            avg_diff[idx] = current_avg - previous_avg

    acol: list[float | None] = [None] * len(close_list)
    for idx in range(norm_window - 1, len(close_list)):
        window_raw = avg_diff[idx - norm_window + 1 : idx + 1]
        if any(value is None for value in window_raw):
            continue
        window = [float(value) for value in window_raw if value is not None]
        denom = max(abs(value) for value in window)
        current_diff = avg_diff[idx]
        if abs(denom) <= 1e-12 or current_diff is None:
            continue
        acol[idx] = current_diff / denom

    valid = [value for value in acol if value is not None]
    if len(valid) < 2:
        return None, None, None

    current = valid[-1]
    previous = valid[-2]
    if current > threshold:
        return True, current, previous
    if current < -threshold:
        return False, current, previous
    return None, current, previous


def _compute_indi2(
    close_list: list[float],
    lookback: int,
    percentile: float,
) -> tuple[bool | None, float | None, float | None, float | None]:
    if len(close_list) < lookback + 1:
        return None, None, None, None

    window = close_list[-lookback:]
    poc = _median(window)
    lower_percentile = (100.0 - percentile) / 2.0
    upper_percentile = 100.0 - lower_percentile
    lower = _percentile_linear(window, lower_percentile)
    upper = _percentile_linear(window, upper_percentile)
    close = close_list[-1]

    if close > poc:
        return True, poc, lower, upper
    if close < poc:
        return False, poc, lower, upper
    return None, poc, lower, upper


def compute_bangoc_dot_color(close_list: list[float]) -> bool | None:
    """Returns only Indi1's dot colour, deliberately ignoring the Indi2 zone."""
    dot_green, _, _ = _compute_indi1(
        close_list=close_list,
        sma_len=settings.INDI1_SMA_LEN,
        norm_window=settings.INDI1_NORM_WINDOW,
        threshold=settings.INDI1_THRESHOLD,
    )
    return dot_green


def is_m15_signal_allowed_by_h1_dot(
    m15_side: BangocSide,
    h1_dot_green: bool | None,
) -> bool:
    """Allows an M15 side only when the current H1 dot has the same colour."""
    return h1_dot_green is not None and (m15_side == "LONG") == h1_dot_green


def compute_bangoc_indicators(close_list: list[float]) -> BangocIndicators | None:
    if not close_list:
        return None

    indi1_green, acol, acol_prev = _compute_indi1(
        close_list=close_list,
        sma_len=settings.INDI1_SMA_LEN,
        norm_window=settings.INDI1_NORM_WINDOW,
        threshold=settings.INDI1_THRESHOLD,
    )
    indi2_green, poc, lower, upper = _compute_indi2(
        close_list=close_list,
        lookback=settings.INDI2_LOOKBACK,
        percentile=settings.INDI2_PERCENTILE,
    )

    if (
        indi1_green is None
        or indi2_green is None
        or acol is None
        or acol_prev is None
        or poc is None
        or lower is None
        or upper is None
    ):
        return None

    side: BangocSide | None = None
    if indi1_green and indi2_green:
        side = "LONG"
    elif not indi1_green and not indi2_green:
        side = "SHORT"

    return BangocIndicators(
        side=side,
        close=close_list[-1],
        indi1_green=indi1_green,
        indi1_acol=acol,
        indi1_acol_prev=acol_prev,
        indi2_green=indi2_green,
        indi2_poc=poc,
        indi2_lower=lower,
        indi2_upper=upper,
    )

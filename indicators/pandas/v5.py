from __future__ import annotations

from typing import Optional


def _prefix(vals: list[float]) -> list[float]:
    out = [0.0]
    total = 0.0
    for value in vals:
        total += value
        out.append(total)
    return out


def _window_avg(prefix: list[float], end_index: int, period: int) -> Optional[float]:
    start = end_index - period + 1
    if start < 0:
        return None
    return (prefix[end_index + 1] - prefix[start]) / period


def _adiff_at(prefix: list[float], index: int, sma_len: int) -> Optional[float]:
    avg = _window_avg(prefix, index, sma_len)
    prev = _window_avg(prefix, index - 5, sma_len)
    if avg is None or prev is None:
        return None
    return avg - prev


def _median_tail(vals: list[float], period: int) -> Optional[float]:
    if len(vals) < period:
        return None
    window = sorted(vals[-period:])
    mid = period // 2
    if period % 2 == 0:
        return (window[mid - 1] + window[mid]) / 2
    return window[mid]


def _atr_tail(high: list[float], low: list[float], close: list[float], period: int) -> Optional[float]:
    n = len(close)
    if n <= period:
        return None
    total = 0.0
    for i in range(n - period, n):
        total += max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    return total / period


def compute_v5_tail_indicators(
    close_list: list[float],
    high_list: list[float],
    low_list: list[float],
    *,
    sma_len: int,
    atr_len: int,
    poc_len: int,
    norm_window: int,
) -> Optional[dict]:
    n = len(close_list)
    if n < norm_window + sma_len + 10:
        return None

    prefix = _prefix(close_list)
    acol_values: list[Optional[float]] = []
    for target in (n - 2, n - 1):
        ds: list[float] = []
        start = target - norm_window + 1
        for index in range(start, target + 1):
            adiff = _adiff_at(prefix, index, sma_len)
            if adiff is not None:
                ds.append(adiff)
        current_adiff = _adiff_at(prefix, target, sma_len)
        abs_max = max((abs(value) for value in ds), default=0.0)
        if current_adiff is None or abs_max <= 1e-12:
            acol_values.append(None)
        else:
            acol_values.append(current_adiff / abs_max)

    atr = _atr_tail(high_list, low_list, close_list, atr_len)
    poc = _median_tail(close_list, poc_len)
    acol_prev, acol = acol_values
    if None in (acol, acol_prev, atr, poc) or atr is None or atr <= 0:
        return None

    return {
        "acol": float(acol),
        "acol_prev": float(acol_prev),
        "atr": float(atr),
        "poc": float(poc),
        "close": float(close_list[-1]),
        "high": float(high_list[-1]),
        "low": float(low_list[-1]),
    }

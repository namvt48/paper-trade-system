"""Numpy-vectorized implementation of V5 tail indicators.

Drop-in replacement for the pure-Python ``compute_v5_tail_indicators`` in
``v5_indicators.py``.  Uses numpy array operations for SMA, ATR, median, and
acol normalization — ~100x faster than the prefix-sum / sorted / deque approach
for 400-500 bar arrays.

Algorithm equivalence is validated by ``tests/test_v5_numpy.py`` against the
pure-Python reference.
"""
from __future__ import annotations

from typing import Optional

import numpy as np


def compute_v5_tail_indicators_numpy(
    close_list: list[float],
    high_list: list[float],
    low_list: list[float],
    *,
    sma_len: int,
    atr_len: int,
    poc_len: int,
    norm_window: int,
) -> Optional[dict]:
    """Compute V5 tail indicators using numpy array operations.

    Returns the same dict shape as the pure-Python version:
    ``{acol, acol_prev, atr, poc, close, high, low}`` or ``None``.
    """
    n = len(close_list)
    if n < norm_window + sma_len + 10:
        return None

    close = np.asarray(close_list, dtype=np.float64)
    high = np.asarray(high_list, dtype=np.float64)
    low = np.asarray(low_list, dtype=np.float64)

    # ── SMA via prefix sum ──────────────────────────────────────────────────
    # prefix[0] = 0, prefix[i] = sum(close[0..i-1])
    prefix = np.empty(n + 1, dtype=np.float64)
    prefix[0] = 0.0
    np.cumsum(close, out=prefix[1:])

    sma = np.full(n, np.nan, dtype=np.float64)
    # SMA[i] = (prefix[i+1] - prefix[i-sma_len+1]) / sma_len, valid for i >= sma_len-1
    first_valid = sma_len - 1
    if first_valid < n:
        sma[first_valid:] = (prefix[first_valid + 1:] - prefix[:n - first_valid]) / sma_len

    # ── adiff = SMA[i] - SMA[i-5] ────────────────────────────────────────────
    adiff = np.full(n, np.nan, dtype=np.float64)
    if n > 5:
        np.subtract(sma[5:], sma[:-5], out=adiff[5:])

    # ── acol for last 2 bars ────────────────────────────────────────────────
    # acol[target] = adiff[target] / max(abs(adiff[target-norm_window+1..target]))
    acol_values: list[Optional[float]] = []
    for target in (n - 2, n - 1):
        start = target - norm_window + 1
        window = adiff[start:target + 1]
        valid = window[np.isfinite(window)]
        if valid.size == 0:
            acol_values.append(None)
            continue
        abs_max = float(np.max(np.abs(valid)))
        current_adiff = adiff[target]
        if not np.isfinite(current_adiff) or abs_max <= 1e-12:
            acol_values.append(None)
        else:
            acol_values.append(float(current_adiff) / abs_max)

    acol_prev, acol = acol_values
    if acol_prev is None or acol is None:
        return None

    # ── ATR (simple mean of True Range over last atr_len bars) ──────────────
    if n <= atr_len:
        return None
    # TR[i] = max(high[i]-low[i], |high[i]-close[i-1]|, |low[i]-close[i-1]|)
    prev_close = np.empty(n, dtype=np.float64)
    prev_close[0] = close[0]
    prev_close[1:] = close[:-1]
    tr = np.maximum(
        high - low,
        np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)),
    )
    atr = float(np.mean(tr[-atr_len:]))
    if atr <= 0:
        return None

    # ── POC (median of last poc_len closes) ─────────────────────────────────
    if n < poc_len:
        return None
    poc = float(np.median(close[-poc_len:]))

    return {
        "acol": float(acol),
        "acol_prev": float(acol_prev),
        "atr": atr,
        "poc": poc,
        "close": float(close[-1]),
        "high": float(high[-1]),
        "low": float(low[-1]),
    }

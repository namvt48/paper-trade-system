"""Indicator logic for the alpha2-NR-Long-Short reversal strategy.

Two indicators (ports of the Pine scripts in ``docs/alphas/indi1.txt`` and
``docs/alphas/indi2.txt``) are reduced to a per-bar color:

* ``indi1`` — EMA cross (fast/slow). Green when ``ema_fast > ema_slow``.
* ``indi2`` — Hull Butterfly oscillator (``hso``). Green when ``hso > 0``.

A combined signal is derived from the two colors:

* both green  -> ``long``
* both red    -> ``short``
* otherwise   -> ``none`` (no position change)
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.config import settings


# ── indi1: EMA cross color ──────────────────────────────────────────────────

def ema_cross_color(close: pd.Series, fast: int, slow: int) -> np.ndarray:
    """Green when EMA(fast) > EMA(slow), red when EMA(fast) < EMA(slow).

    Mirrors the fill color of the "Giai cuu the gioi" Pine strategy
    (``ema1 > ema2 ? color1 : color2``).
    """
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    diff = ema_fast.to_numpy() - ema_slow.to_numpy()
    color = np.where(diff > 0, "green", np.where(diff < 0, "red", "none"))
    color = np.where(np.isnan(diff), "none", color)
    return color.astype(object)


# ── indi2: Hull Butterfly oscillator ─────────────────────────────────────────

def _hull_coeffs(length: int) -> list[float]:
    """Precompute the Hull Butterfly convolution coefficients (Pine port).

    Reproduces ``lcwa_coeffs`` + ``hull_coeffs`` from indi2.txt exactly,
    including Pine ``int()`` truncation semantics for ``short_len``/``hull_len``.
    """
    short_len = int(length / 2)          # Pine int() truncates toward zero
    hull_len = int(np.sqrt(length))      # Pine int() truncates toward zero
    den1 = short_len * (short_len + 1) / 2
    den2 = length * (length + 1) / 2
    den3 = hull_len * (hull_len + 1) / 2

    # Linearly combined WMA coefficients (built via unshift/prepend).
    lcwa: list[float] = []
    for i in range(length):
        sum1 = max(short_len - i, 0)
        sum2 = length - i
        lcwa.insert(0, 2 * (sum1 / den1) - (sum2 / den2))
    # Zero padding of linearly combined WMA coeffs.
    for _ in range(hull_len - 1):
        lcwa.insert(0, 0.0)

    # WMA convolution of the linearly combined WMA coeffs.
    hull: list[float] = []
    for i in range(hull_len, len(lcwa)):
        s = 0.0
        for j in range(i - hull_len, i):
            s += lcwa[j] * (i - j)
        hull.insert(0, s / den3)
    return hull


def hull_butterfly_hso(close: np.ndarray, length: int = 14) -> np.ndarray:
    """Hull Butterfly oscillator (``hso``) from indi2.txt.

    ``hma[t]  = sum_i hull[i] * close[t-i]``
    ``inv_hma[t] = sum_i hull[i] * close[t-(L-1)+i]``
    ``hso[t] = hma[t] - inv_hma[t]``

    Positive => green histogram, negative => red.
    """
    hull = _hull_coeffs(length)
    L = len(hull)
    n = len(close)
    arr = np.asarray(close, dtype=float)
    hso = np.full(n, np.nan)
    for t in range(L - 1, n):
        seg = arr[t - L + 1: t + 1]          # close[t-L+1 .. t]
        hma = float(np.dot(hull, seg[::-1]))  # hull[i] * close[t-i]
        inv_hma = float(np.dot(hull, seg))    # hull[i] * close[t-L+1+i]
        hso[t] = hma - inv_hma
    return hso


def hull_butterfly_color(close: np.ndarray, length: int = 14) -> np.ndarray:
    """Green when ``hso > 0``, red when ``hso < 0`` (indi2 histogram color)."""
    hso = hull_butterfly_hso(close, length)
    color = np.where(
        np.isnan(hso), "none",
        np.where(hso > 0, "green", np.where(hso < 0, "red", "none")),
    )
    return color.astype(object)


# ── Combined signal ──────────────────────────────────────────────────────────

def combined_signal(c1: np.ndarray, c2: np.ndarray) -> np.ndarray:
    """Both green => ``long``, both red => ``short``, otherwise ``none``."""
    return np.where(
        (c1 == "green") & (c2 == "green"), "long",
        np.where((c1 == "red") & (c2 == "red"), "short", "none"),
    ).astype(object)


__all__ = [
    "ema_cross_color",
    "hull_butterfly_hso",
    "hull_butterfly_color",
    "combined_signal",
]

"""Equivalence tests for the numpy-backed v5 indicator implementation.

Validates that ``compute_v5_tail_indicators`` (which now delegates to the numpy
backend in ``v5_numpy.py``) produces results identical to the pure-Python
reference implementation for a range of inputs and edge cases.
"""
from __future__ import annotations

import math
import random

import pytest

from base.tests.test_v5_indicators import _reference
from base.v5_indicators import compute_v5_tail_indicators

SMA_LEN = 50
ATR_LEN = 200
POC_LEN = 30
NORM_WINDOW = 252


def _gen_series(seed: int, n: int, scale: float = 0.2) -> tuple[list[float], list[float], list[float]]:
    random.seed(seed)
    close = [100.0 + math.sin(i / 7) + random.random() * scale for i in range(n)]
    high = [v + 0.5 + random.random() * 0.1 for v in close]
    low = [v - 0.5 - random.random() * 0.1 for v in close]
    return close, high, low


def test_numpy_insufficient_data():
    """Returns None when fewer bars than the minimum threshold."""
    close = [100.0] * 100
    high = [101.0] * 100
    low = [99.0] * 100
    result = compute_v5_tail_indicators(
        close, high, low,
        sma_len=SMA_LEN, atr_len=ATR_LEN, poc_len=POC_LEN, norm_window=NORM_WINDOW,
    )
    assert result is None  # 100 < 252+50+10 = 312


def test_numpy_matches_reference_seed7():
    """numpy backend must match pure-Python reference (seed=7, 420 bars)."""
    close, high, low = _gen_series(seed=7, n=420)
    actual = compute_v5_tail_indicators(
        close, high, low,
        sma_len=SMA_LEN, atr_len=ATR_LEN, poc_len=POC_LEN, norm_window=NORM_WINDOW,
    )
    expected = _reference(close, high, low)
    assert actual is not None
    assert expected is not None
    for key, expected_value in expected.items():
        assert actual[key] == pytest.approx(expected_value, rel=1e-9, abs=1e-12)


def test_numpy_matches_reference_seed42():
    """numpy backend must match pure-Python reference (seed=42, 350 bars)."""
    close, high, low = _gen_series(seed=42, n=350, scale=0.3)
    actual = compute_v5_tail_indicators(
        close, high, low,
        sma_len=SMA_LEN, atr_len=ATR_LEN, poc_len=POC_LEN, norm_window=NORM_WINDOW,
    )
    expected = _reference(close, high, low)
    assert actual is not None
    assert expected is not None
    for key, expected_value in expected.items():
        assert actual[key] == pytest.approx(expected_value, rel=1e-9, abs=1e-12)


def test_numpy_boundary_exact_min_bars():
    """Boundary: exactly norm_window + sma_len + 10 bars (=312)."""
    min_bars = NORM_WINDOW + SMA_LEN + 10
    close, high, low = _gen_series(seed=99, n=min_bars, scale=0.15)
    actual = compute_v5_tail_indicators(
        close, high, low,
        sma_len=SMA_LEN, atr_len=ATR_LEN, poc_len=POC_LEN, norm_window=NORM_WINDOW,
    )
    expected = _reference(close, high, low)
    if expected is None:
        assert actual is None
    else:
        assert actual is not None
        for key, expected_value in expected.items():
            assert actual[key] == pytest.approx(expected_value, rel=1e-9, abs=1e-12)


def test_numpy_flat_prices():
    """Returns None when prices are flat (acol=0, atr=0)."""
    close = [100.0] * 420
    high = [100.0] * 420
    low = [100.0] * 420
    result = compute_v5_tail_indicators(
        close, high, low,
        sma_len=SMA_LEN, atr_len=ATR_LEN, poc_len=POC_LEN, norm_window=NORM_WINDOW,
    )
    assert result is None


def test_numpy_large_series():
    """numpy backend must match reference with 500 bars (production scale)."""
    close, high, low = _gen_series(seed=123, n=500, scale=0.25)
    actual = compute_v5_tail_indicators(
        close, high, low,
        sma_len=SMA_LEN, atr_len=ATR_LEN, poc_len=POC_LEN, norm_window=NORM_WINDOW,
    )
    expected = _reference(close, high, low)
    assert actual is not None
    assert expected is not None
    for key, expected_value in expected.items():
        assert actual[key] == pytest.approx(expected_value, rel=1e-9, abs=1e-12)

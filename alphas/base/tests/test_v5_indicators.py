import math
import random
from typing import Optional

import pytest

from base.v5_indicators import V5SymbolState, compute_v5_tail_indicators


def _calc_sma(vals: list[float], p: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(vals)
    total = 0.0
    for i, value in enumerate(vals):
        total += value
        if i >= p:
            total -= vals[i - p]
        if i >= p - 1:
            out[i] = total / p
    return out


def _reference(close: list[float], high: list[float], low: list[float]) -> Optional[dict]:
    sma_len = 50
    atr_len = 200
    poc_len = 30
    norm_window = 252
    n = len(close)
    if n < norm_window + sma_len + 10:
        return None
    avg = _calc_sma(close, sma_len)
    trs = [0.0] * n
    for i in range(1, n):
        trs[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    atr: list[Optional[float]] = [None] * n
    total = 0.0
    for i in range(1, n):
        total += trs[i]
        if i > atr_len:
            total -= trs[i - atr_len]
        if i >= atr_len:
            atr[i] = total / atr_len

    med: list[Optional[float]] = [None] * n
    for i in range(poc_len - 1, n):
        window = sorted(close[i - poc_len + 1: i + 1])
        mid = poc_len // 2
        med[i] = (window[mid - 1] + window[mid]) / 2

    adiff: list[Optional[float]] = [None] * n
    for i in range(5, n):
        if avg[i] is not None and avg[i - 5] is not None:
            adiff[i] = avg[i] - avg[i - 5]  # type: ignore[operator]

    acol: list[Optional[float]] = [None] * n
    for i in range(norm_window, n):
        ds = [d for d in adiff[i - norm_window + 1: i + 1] if d is not None]
        if ds:
            abs_max = max(abs(v) for v in ds)
            if abs_max > 1e-12 and adiff[i] is not None:
                acol[i] = adiff[i] / abs_max  # type: ignore[operator]

    if None in (acol[-1], acol[-2], atr[-1], med[-1]) or atr[-1] <= 0:  # type: ignore[operator]
        return None
    return {
        "acol": float(acol[-1]),
        "acol_prev": float(acol[-2]),
        "atr": float(atr[-1]),
        "poc": float(med[-1]),
        "close": float(close[-1]),
        "high": float(high[-1]),
        "low": float(low[-1]),
    }


def test_compute_v5_tail_indicators_matches_reference():
    random.seed(7)
    close = [100.0 + math.sin(i / 7) + random.random() * 0.2 for i in range(420)]
    high = [v + 0.5 + random.random() * 0.1 for v in close]
    low = [v - 0.5 - random.random() * 0.1 for v in close]
    actual = compute_v5_tail_indicators(
        close,
        high,
        low,
        sma_len=50,
        atr_len=200,
        poc_len=30,
        norm_window=252,
    )
    expected = _reference(close, high, low)
    assert actual is not None
    assert expected is not None
    for key, expected_value in expected.items():
        assert actual[key] == pytest.approx(expected_value)


def test_v5_symbol_state_matches_reference_tail():
    random.seed(11)
    close = [100.0 + math.sin(i / 9) + random.random() * 0.2 for i in range(420)]
    high = [v + 0.4 + random.random() * 0.1 for v in close]
    low = [v - 0.4 - random.random() * 0.1 for v in close]
    state = V5SymbolState(sma_len=50, atr_len=200, poc_len=30, norm_window=252)
    actual = None
    for c, h, l in zip(close, high, low):
        actual = state.append(c, h, l)
    expected = _reference(close, high, low)
    assert actual is not None
    assert expected is not None
    for key, expected_value in expected.items():
        assert actual[key] == pytest.approx(expected_value)

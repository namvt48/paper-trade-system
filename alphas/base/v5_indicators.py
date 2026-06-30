from __future__ import annotations

from bisect import insort
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from base.v5_numpy import compute_v5_tail_indicators_numpy


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
    """Compute V5 tail indicators.

    Delegates to the numpy-vectorized backend in ``v5_numpy.py`` for
    ~100x speedup over the pure-Python prefix-sum implementation.
    The pure-Python helpers above are retained for reference and
    fallback testing.
    """
    return compute_v5_tail_indicators_numpy(
        close_list,
        high_list,
        low_list,
        sma_len=sma_len,
        atr_len=atr_len,
        poc_len=poc_len,
        norm_window=norm_window,
    )


@dataclass
class V5SymbolState:
    """Incremental V5 indicator cache for future engine-level scans.

    This is intentionally isolated from engine position logic. The current
    engines can use the tail-only function above while tests validate this
    state object against the same output before a broader engine refactor.
    """

    sma_len: int
    atr_len: int
    poc_len: int
    norm_window: int
    close: list[float] = field(default_factory=list)
    high: list[float] = field(default_factory=list)
    low: list[float] = field(default_factory=list)
    _sma_sum: float = 0.0
    _sma_values: list[Optional[float]] = field(default_factory=list)
    _tr_sum: float = 0.0
    _tr_window: deque[float] = field(default_factory=deque)
    _poc_sorted: list[float] = field(default_factory=list)
    _adiff_window: deque[float] = field(default_factory=deque)
    _last_acol: Optional[float] = None

    def append(self, close: float, high: float, low: float) -> Optional[dict]:
        prev_close = self.close[-1] if self.close else close
        self.close.append(close)
        self.high.append(high)
        self.low.append(low)

        self._sma_sum += close
        if len(self.close) > self.sma_len:
            self._sma_sum -= self.close[-self.sma_len - 1]
        current_sma = self._sma_sum / self.sma_len if len(self.close) >= self.sma_len else None
        self._sma_values.append(current_sma)

        current_acol: Optional[float] = None
        if current_sma is not None and len(self._sma_values) > 5:
            prev_sma = self._sma_values[-6]
            if prev_sma is not None:
                adiff = current_sma - prev_sma
                self._adiff_window.append(adiff)
                if len(self._adiff_window) > self.norm_window:
                    self._adiff_window.popleft()
                abs_max = max((abs(value) for value in self._adiff_window), default=0.0)
                if abs_max > 1e-12:
                    current_acol = adiff / abs_max

        tr = max(high - low, abs(high - prev_close), abs(low - prev_close)) if len(self.close) > 1 else 0.0
        self._tr_sum += tr
        self._tr_window.append(tr)
        if len(self._tr_window) > self.atr_len:
            self._tr_sum -= self._tr_window.popleft()

        insort(self._poc_sorted, close)
        if len(self.close) > self.poc_len:
            old = self.close[-self.poc_len - 1]
            idx = self._poc_sorted.index(old)
            self._poc_sorted.pop(idx)

        if (
            len(self.close) < self.norm_window + self.sma_len + 10
            or current_acol is None
            or self._last_acol is None
            or len(self._tr_window) < self.atr_len
            or len(self._poc_sorted) < self.poc_len
        ):
            self._last_acol = current_acol
            return None

        atr = self._tr_sum / self.atr_len
        if atr <= 0:
            self._last_acol = current_acol
            return None

        mid = self.poc_len // 2
        if self.poc_len % 2 == 0:
            poc = (self._poc_sorted[mid - 1] + self._poc_sorted[mid]) / 2
        else:
            poc = self._poc_sorted[mid]

        result = {
            "acol": float(current_acol),
            "acol_prev": float(self._last_acol),
            "atr": float(atr),
            "poc": float(poc),
            "close": float(close),
            "high": float(high),
            "low": float(low),
        }
        self._last_acol = current_acol
        return result

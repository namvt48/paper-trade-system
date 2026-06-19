# Indicators Library Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a unified indicators library at `paper-trade-system/indicators/` with streaming core + pandas wrappers, ported from existing code.

**Architecture:** Streaming classes (stateful, O(1)/bar) ported from `signal_engine.py`. Pandas batch functions (stateless) ported from `cross_alpha/strategy.py` and alpha stores. V5 indicators ported from `base/v5_indicators.py`. No existing code changed — lib is additive.

**Tech Stack:** Python 3, numpy, pandas, collections.deque

---

### Task 1: Create directory structure and `__init__.py` files

**Files:**
- Create: `indicators/__init__.py`
- Create: `indicators/streaming/__init__.py`
- Create: `indicators/pandas/__init__.py`
- Create: `indicators/tests/__init__.py`

- [ ] **Step 1: Create directories**

```bash
mkdir -p indicators/streaming indicators/pandas indicators/tests
```

- [ ] **Step 2: Write `indicators/__init__.py`**

```python
from indicators.streaming import EMA, RollingMoments, DecayLinear, RollingExtreme, Momentum
from indicators.pandas import ts_mean, ts_std, ts_zscore, ts_skew, ts_momentum, decay_linear
```

- [ ] **Step 3: Write `indicators/streaming/__init__.py`**

```python
from indicators.streaming.moments import RollingMoments
from indicators.streaming.ema import EMA
from indicators.streaming.decay import DecayLinear
from indicators.streaming.extreme import RollingExtreme
from indicators.streaming.momentum import Momentum
from indicators.streaming.cross_sectional import cs_zscore, cs_demean, cs_winsorize, cs_scale, cs_rank
from indicators.streaming.v5 import V5SymbolState
```

- [ ] **Step 4: Write `indicators/pandas/__init__.py`**

```python
from indicators.pandas.ts_ops import ts_mean, ts_std, ts_zscore, ts_skew, ts_momentum, decay_linear
from indicators.pandas.cs_ops import cs_zscore, cs_demean, cs_winsorize, cs_scale, rank
from indicators.pandas.v5 import compute_v5_tail_indicators
from indicators.pandas.technical import sma, atr, bollinger_bands
```

- [ ] **Step 5: Write `indicators/tests/__init__.py`**

Empty file.

- [ ] **Step 6: Verify imports don't fail yet (will fail since modules don't exist)**

Just verify directory structure exists:

```bash
ls indicators/ indicators/streaming/ indicators/pandas/ indicators/tests/
```

- [ ] **Step 7: Commit**

```bash
git add indicators/
git commit -m "feat: create indicators library directory structure"
```

---

### Task 2: Streaming `RollingMoments`

**Files:**
- Create: `indicators/streaming/moments.py`
- Create: `indicators/tests/test_streaming_moments.py`

- [ ] **Step 1: Write failing tests**

```python
import math
from indicators.streaming.moments import RollingMoments


def test_mean_full_window():
    rm = RollingMoments(3)
    rm.update(2.0); rm.update(4.0); rm.update(6.0)
    assert rm.mean() == 4.0


def test_mean_partial_window():
    rm = RollingMoments(5)
    rm.update(2.0); rm.update(4.0)
    assert rm.mean() == 3.0


def test_mean_sliding_window():
    rm = RollingMoments(3)
    rm.update(1.0); rm.update(2.0); rm.update(3.0); rm.update(4.0)
    assert rm.mean() == 3.0


def test_std_ddof1():
    rm = RollingMoments(4)
    rm.update(2.0); rm.update(4.0); rm.update(4.0); rm.update(4.0)
    assert abs(rm.std() - 1.0) < 1e-10


def test_std_less_than_two_returns_nan():
    rm = RollingMoments(5)
    rm.update(1.0)
    assert math.isnan(rm.std())


def test_zscore():
    rm = RollingMoments(4)
    rm.update(2.0); rm.update(4.0); rm.update(4.0); rm.update(4.0)
    z = rm.zscore(3.0)
    assert abs(z - (-1.0)) < 1e-10


def test_skew():
    rm = RollingMoments(4)
    rm.update(1.0); rm.update(2.0); rm.update(5.0); rm.update(8.0)
    s = rm.skew()
    assert not math.isnan(s)


def test_skew_less_than_three_returns_nan():
    rm = RollingMoments(5)
    rm.update(1.0); rm.update(2.0)
    assert math.isnan(rm.skew())


def test_nan_does_not_advance_window():
    rm = RollingMoments(3)
    rm.update(1.0); rm.update(float("nan")); rm.update(3.0)
    assert rm.mean() == 2.0


def test_update_returns_self():
    rm = RollingMoments(3)
    assert rm.update(1.0) is rm


def test_empty_mean_is_nan():
    rm = RollingMoments(3)
    assert math.isnan(rm.mean())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest indicators/tests/test_streaming_moments.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Write implementation**

```python
from __future__ import annotations

from collections import deque


class RollingMoments:
    """Window mean / std(ddof=1) / zscore / skew via running power sums."""
    __slots__ = ("d", "buf", "s1", "s2", "s3")

    def __init__(self, d: int):
        self.d = d
        self.buf: deque[float] = deque()
        self.s1 = self.s2 = self.s3 = 0.0

    def update(self, x: float) -> RollingMoments:
        if x != x:
            return self
        b = self.buf
        b.append(x)
        self.s1 += x
        self.s2 += x * x
        self.s3 += x * x * x
        if len(b) > self.d:
            o = b.popleft()
            self.s1 -= o
            self.s2 -= o * o
            self.s3 -= o * o * o
        return self

    def mean(self) -> float:
        n = len(self.buf)
        return self.s1 / n if n else float("nan")

    def std(self) -> float:
        n = len(self.buf)
        if n < 2:
            return float("nan")
        m = self.s1 / n
        v = (self.s2 - n * m * m) / (n - 1)
        return v ** 0.5 if v > 0 else float("nan")

    def zscore(self, x: float) -> float:
        s = self.std()
        return (x - self.mean()) / s if s == s and s > 0 else float("nan")

    def skew(self) -> float:
        n = len(self.buf)
        if n < 3:
            return float("nan")
        m = self.s1 / n
        m2 = self.s2 / n - m * m
        m3 = self.s3 / n - 3 * m * (self.s2 / n) + 2 * m * m * m
        return m3 / m2 ** 1.5 if m2 > 1e-12 else float("nan")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest indicators/tests/test_streaming_moments.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add indicators/streaming/moments.py indicators/tests/test_streaming_moments.py
git commit -m "feat: add streaming RollingMoments indicator"
```

---

### Task 3: Streaming `EMA`

**Files:**
- Create: `indicators/streaming/ema.py`
- Create: `indicators/tests/test_streaming_ema.py`

- [ ] **Step 1: Write failing tests**

```python
import math
from indicators.streaming.ema import EMA


def test_ema_converges():
    ema = EMA(10)
    for _ in range(20):
        ema.update(100.0)
    assert abs(ema.value() - 100.0) < 1e-10


def test_ema_min_periods():
    ema = EMA(10)
    ema.update(100.0)
    assert math.isnan(ema.value())
    for _ in range(4):
        ema.update(100.0)
    assert not math.isnan(ema.value())


def test_ema_nan_skipped():
    ema = EMA(10)
    for _ in range(5):
        ema.update(100.0)
    ema.update(float("nan"))
    assert abs(ema.value() - 100.0) < 1e-10


def test_ema_update_returns_self():
    ema = EMA(10)
    assert ema.update(1.0) is ema
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest indicators/tests/test_streaming_ema.py -v`
Expected: FAIL

- [ ] **Step 3: Write implementation**

```python
from __future__ import annotations


class EMA:
    __slots__ = ("a", "y", "n", "minp")

    def __init__(self, span: int):
        self.a = 2.0 / (span + 1.0)
        self.y: float | None = None
        self.n = 0
        self.minp = max(1, span // 2)

    def update(self, x: float) -> EMA:
        if x == x:
            self.y = x if self.y is None else self.a * x + (1 - self.a) * self.y
            self.n += 1
        return self

    def value(self) -> float:
        return self.y if self.n >= self.minp else float("nan")
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest indicators/tests/test_streaming_ema.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add indicators/streaming/ema.py indicators/tests/test_streaming_ema.py
git commit -m "feat: add streaming EMA indicator"
```

---

### Task 4: Streaming `DecayLinear`

**Files:**
- Create: `indicators/streaming/decay.py`
- Create: `indicators/tests/test_streaming_decay.py`

- [ ] **Step 1: Write failing tests**

```python
import math
from indicators.streaming.decay import DecayLinear


def test_decay_linear_not_full_returns_nan():
    dl = DecayLinear(3)
    dl.update(1.0)
    assert math.isnan(dl.value())


def test_decay_linear_full_window():
    dl = DecayLinear(3)
    dl.update(1.0); dl.update(2.0); dl.update(3.0)
    # weights: 3*1 + 2*2 + 1*1 → but order is newest=3, so: 3*3 + 2*2 + 1*1 = 14
    # norm = 3*4/2 = 6
    # Actually: buf = [1, 2, 3], weights are d..1 from newest: 3*3 + 2*2 + 1*1 = 14, norm = 6
    assert abs(dl.value() - 14.0 / 6.0) < 1e-10


def test_decay_linear_sliding():
    dl = DecayLinear(3)
    dl.update(1.0); dl.update(2.0); dl.update(3.0); dl.update(4.0)
    # buf = [2, 3, 4], weights: 3*4 + 2*3 + 1*2 = 20, norm = 6
    assert abs(dl.value() - 20.0 / 6.0) < 1e-10


def test_decay_linear_nan_skipped():
    dl = DecayLinear(3)
    dl.update(1.0); dl.update(float("nan")); dl.update(3.0)
    assert math.isnan(dl.value())


def test_decay_linear_update_returns_self():
    dl = DecayLinear(3)
    assert dl.update(1.0) is dl
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest indicators/tests/test_streaming_decay.py -v`
Expected: FAIL

- [ ] **Step 3: Write implementation**

```python
from __future__ import annotations

from collections import deque


class DecayLinear:
    """Linearly-weighted MA, newest weight d..oldest 1. O(1): WS = WS - S + d*x."""
    __slots__ = ("d", "buf", "S", "WS", "norm")

    def __init__(self, d: int):
        self.d = d
        self.buf: deque[float] = deque()
        self.S = 0.0
        self.WS = 0.0
        self.norm = d * (d + 1) / 2.0

    def update(self, x: float) -> DecayLinear:
        if x != x:
            return self
        d, b = self.d, self.buf
        if len(b) == d:
            old = b[0]
            self.WS = self.WS - self.S + d * x
            self.S = self.S - old + x
            b.append(x)
            b.popleft()
        else:
            b.append(x)
            self.S += x
            if len(b) == d:
                self.WS = sum((j + 1) * b[j] for j in range(d))
        return self

    def value(self) -> float:
        return self.WS / self.norm if len(self.buf) == self.d else float("nan")
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest indicators/tests/test_streaming_decay.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add indicators/streaming/decay.py indicators/tests/test_streaming_decay.py
git commit -m "feat: add streaming DecayLinear indicator"
```

---

### Task 5: Streaming `RollingExtreme`

**Files:**
- Create: `indicators/streaming/extreme.py`
- Create: `indicators/tests/test_streaming_extreme.py`

- [ ] **Step 1: Write failing tests**

```python
import math
from indicators.streaming.extreme import RollingExtreme


def test_rolling_max():
    re = RollingExtreme(3, is_max=True)
    re.update(1.0); re.update(3.0); re.update(2.0)
    assert re.value() == 3.0


def test_rolling_max_slides():
    re = RollingExtreme(3, is_max=True)
    re.update(1.0); re.update(3.0); re.update(2.0); re.update(1.0)
    assert re.value() == 3.0


def test_rolling_max_expires():
    re = RollingExtreme(3, is_max=True)
    re.update(1.0); re.update(3.0); re.update(2.0); re.update(1.0); re.update(0.5)
    assert re.value() == 2.0


def test_rolling_min():
    re = RollingExtreme(3, is_max=False)
    re.update(3.0); re.update(1.0); re.update(2.0)
    assert re.value() == 1.0


def test_not_enough_data_returns_nan():
    re = RollingExtreme(3)
    re.update(1.0); re.update(2.0)
    assert math.isnan(re.value())


def test_nan_skipped():
    re = RollingExtreme(3)
    re.update(1.0); re.update(float("nan")); re.update(3.0)
    assert math.isnan(re.value())


def test_update_returns_self():
    re = RollingExtreme(3)
    assert re.update(1.0) is re
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest indicators/tests/test_streaming_extreme.py -v`
Expected: FAIL

- [ ] **Step 3: Write implementation**

```python
from __future__ import annotations

from collections import deque


class RollingExtreme:
    """ts_min / ts_max in O(1) amortised via a monotonic deque."""
    __slots__ = ("d", "dq", "t", "is_max")

    def __init__(self, d: int, is_max: bool = True):
        self.d = d
        self.dq: deque[tuple[int, float]] = deque()
        self.t = 0
        self.is_max = is_max

    def update(self, x: float) -> RollingExtreme:
        if x != x:
            return self
        dq = self.dq
        while dq and ((dq[-1][1] <= x) if self.is_max else (dq[-1][1] >= x)):
            dq.pop()
        dq.append((self.t, x))
        while dq[0][0] <= self.t - self.d:
            dq.popleft()
        self.t += 1
        return self

    def value(self) -> float:
        return self.dq[0][1] if self.t >= self.d else float("nan")
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest indicators/tests/test_streaming_extreme.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add indicators/streaming/extreme.py indicators/tests/test_streaming_extreme.py
git commit -m "feat: add streaming RollingExtreme indicator"
```

---

### Task 6: Streaming `Momentum`

**Files:**
- Create: `indicators/streaming/momentum.py`
- Create: `indicators/tests/test_streaming_momentum.py`

- [ ] **Step 1: Write failing tests**

```python
import math
from indicators.streaming.momentum import Momentum


def test_momentum_not_enough_data():
    mom = Momentum(3)
    mom.update(100.0); mom.update(110.0)
    assert math.isnan(mom.value())


def test_momentum_basic():
    mom = Momentum(2)
    mom.update(100.0); mom.update(110.0); mom.update(121.0)
    assert abs(mom.value() - 0.21) < 1e-10


def test_momentum_zero_base():
    mom = Momentum(1)
    mom.update(0.0); mom.update(5.0)
    assert math.isnan(mom.value())


def test_nan_skipped():
    mom = Momentum(2)
    mom.update(100.0); mom.update(float("nan")); mom.update(110.0)
    assert math.isnan(mom.value())


def test_update_returns_self():
    mom = Momentum(3)
    assert mom.update(1.0) is mom
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest indicators/tests/test_streaming_momentum.py -v`
Expected: FAIL

- [ ] **Step 3: Write implementation**

```python
from __future__ import annotations

from collections import deque


class Momentum:
    """ts_momentum(x, d) = x_t / x_{t-d} - 1."""
    __slots__ = ("d", "buf")

    def __init__(self, d: int):
        self.d = d
        self.buf: deque[float] = deque(maxlen=d + 1)

    def update(self, x: float) -> Momentum:
        if x == x:
            self.buf.append(x)
        return self

    def value(self) -> float:
        if len(self.buf) <= self.d:
            return float("nan")
        o = self.buf[0]
        return self.buf[-1] / o - 1.0 if o else float("nan")
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest indicators/tests/test_streaming_momentum.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add indicators/streaming/momentum.py indicators/tests/test_streaming_momentum.py
git commit -m "feat: add streaming Momentum indicator"
```

---

### Task 7: Streaming cross-sectional operators

**Files:**
- Create: `indicators/streaming/cross_sectional.py`
- Create: `indicators/tests/test_streaming_cs.py`

- [ ] **Step 1: Write failing tests**

```python
import numpy as np
from indicators.streaming.cross_sectional import cs_zscore, cs_demean, cs_winsorize, cs_scale, cs_rank


def test_cs_zscore():
    v = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    z = cs_zscore(v)
    assert abs(np.nanmean(z)) < 1e-10
    assert abs(np.nanstd(z, ddof=1) - 1.0) < 1e-10


def test_cs_demean():
    v = np.array([1.0, 2.0, 3.0])
    d = cs_demean(v)
    assert abs(np.nanmean(d)) < 1e-10


def test_cs_winsorize():
    v = np.array([1.0, 2.0, 3.0, 100.0])
    w = cs_winsorize(v, k=2.0)
    assert w.max() < 100.0


def test_cs_scale():
    v = np.array([3.0, 4.0])
    s = cs_scale(v, a=1.0)
    assert abs(np.nansum(np.abs(s)) - 1.0) < 1e-10


def test_cs_rank():
    v = np.array([30.0, 10.0, 20.0])
    r = cs_rank(v)
    assert abs(r[0] - 3 / 3) < 1e-10
    assert abs(r[1] - 1 / 3) < 1e-10
    assert abs(r[2] - 2 / 3) < 1e-10


def test_cs_rank_with_nan():
    v = np.array([3.0, float("nan"), 1.0])
    r = cs_rank(v)
    assert abs(r[0] - 2 / 2) < 1e-10
    assert np.isnan(r[1])
    assert abs(r[2] - 1 / 2) < 1e-10


def test_cs_zscore_zero_std():
    v = np.array([5.0, 5.0, 5.0])
    z = cs_zscore(v)
    assert np.all(z == 0.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest indicators/tests/test_streaming_cs.py -v`
Expected: FAIL

- [ ] **Step 3: Write implementation**

```python
from __future__ import annotations

import numpy as np


def cs_zscore(v: np.ndarray) -> np.ndarray:
    s = np.nanstd(v, ddof=1)
    return (v - np.nanmean(v)) / s if s == s and s > 0 else np.zeros_like(v)


def cs_demean(v: np.ndarray) -> np.ndarray:
    return v - np.nanmean(v)


def cs_winsorize(v: np.ndarray, k: float = 3.0) -> np.ndarray:
    m = np.nanmean(v)
    s = np.nanstd(v, ddof=1)
    return np.clip(v, m - k * s, m + k * s)


def cs_scale(v: np.ndarray, a: float = 1.0) -> np.ndarray:
    s = np.nansum(np.abs(v))
    return v / s * a if s > 0 else np.zeros_like(v)


def cs_rank(v: np.ndarray) -> np.ndarray:
    out = np.full(v.shape, np.nan)
    m = ~np.isnan(v)
    x = v[m]
    if x.size:
        order = x.argsort()
        r = np.empty(x.size)
        r[order] = np.arange(1, x.size + 1)
        out[m] = r / x.size
    return out
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest indicators/tests/test_streaming_cs.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add indicators/streaming/cross_sectional.py indicators/tests/test_streaming_cs.py
git commit -m "feat: add streaming cross-sectional operators"
```

---

### Task 8: Streaming `V5SymbolState`

**Files:**
- Create: `indicators/streaming/v5.py`
- Create: `indicators/tests/test_streaming_v5.py`

- [ ] **Step 1: Write the streaming V5SymbolState class** (no test yet — cross-validation test deferred to Task 10 when batch version exists)

```python
from __future__ import annotations

from bisect import insort
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class V5SymbolState:
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
```

- [ ] **Step 4: Skip test until pandas/v5.py exists (Task 10), then run**

For now, commit the streaming V5 without the cross-validation test (will add in Task 10).

- [ ] **Step 5: Commit**

```bash
git add indicators/streaming/v5.py
git commit -m "feat: add streaming V5SymbolState indicator"
```

---

### Task 9: Pandas time-series operators

**Files:**
- Create: `indicators/pandas/ts_ops.py`
- Create: `indicators/tests/test_pandas_ts_ops.py`

- [ ] **Step 1: Write failing tests**

```python
import numpy as np
import pandas as pd
from indicators.pandas.ts_ops import ts_mean, ts_std, ts_zscore, ts_skew, ts_momentum, decay_linear


def test_ts_mean():
    x = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    result = ts_mean(x, 3)
    assert abs(result.iloc[4] - 4.0) < 1e-10


def test_ts_std():
    x = pd.Series([2.0, 4.0, 4.0, 4.0])
    result = ts_std(x, 4)
    assert abs(result.iloc[3] - 1.0) < 1e-10


def test_ts_zscore():
    x = pd.Series(list(range(1, 21)), dtype=float)
    result = ts_zscore(x, 10)
    assert not result.iloc[-1] != result.iloc[-1]  # not NaN


def test_ts_momentum():
    x = pd.Series([100.0, 110.0, 121.0])
    result = ts_momentum(x, 1)
    assert abs(result.iloc[2] - 0.1) < 1e-10


def test_decay_linear():
    x = pd.Series([1.0, 2.0, 3.0])
    result = decay_linear(x, 2)
    assert not np.isnan(result.iloc[2])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest indicators/tests/test_pandas_ts_ops.py -v`
Expected: FAIL

- [ ] **Step 3: Write implementation**

```python
from __future__ import annotations

import numpy as np
import pandas as pd


def ts_mean(x: pd.Series, d: int) -> pd.Series:
    return x.rolling(d, min_periods=max(1, d // 2)).mean()


def ts_std(x: pd.Series, d: int) -> pd.Series:
    return x.rolling(d, min_periods=max(2, d // 2)).std()


def ts_zscore(x: pd.Series, d: int) -> pd.Series:
    return (x - ts_mean(x, d)) / ts_std(x, d).replace(0, np.nan)


def ts_skew(x: pd.Series, d: int) -> pd.Series:
    return x.rolling(d, min_periods=max(3, d // 2)).skew()


def ts_momentum(x: pd.Series, d: int) -> pd.Series:
    return x / x.shift(d) - 1.0


def decay_linear(x: pd.Series, d: int) -> pd.Series:
    weights = np.arange(d, 0, -1, dtype=float)
    weights /= weights.sum()
    return sum(weights[k] * x.shift(k) for k in range(d))
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest indicators/tests/test_pandas_ts_ops.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add indicators/pandas/ts_ops.py indicators/tests/test_pandas_ts_ops.py
git commit -m "feat: add pandas time-series operators"
```

---

### Task 10: Pandas cross-sectional operators + V5 batch + technical

**Files:**
- Create: `indicators/pandas/cs_ops.py`
- Create: `indicators/pandas/v5.py`
- Create: `indicators/pandas/technical.py`
- Create: `indicators/tests/test_pandas_cs_ops.py`
- Create: `indicators/tests/test_pandas_v5.py`
- Create: `indicators/tests/test_pandas_technical.py`
- Create: `indicators/tests/test_streaming_v5.py` (cross-validation test from Task 8)

- [ ] **Step 1: Write `indicators/pandas/cs_ops.py`**

```python
from __future__ import annotations

import numpy as np
import pandas as pd
from indicators.streaming.cross_sectional import cs_zscore as _cs_zscore, cs_demean as _cs_demean, cs_winsorize as _cs_winsorize, cs_scale as _cs_scale, cs_rank as _cs_rank


def cs_zscore(x: pd.Series) -> pd.Series:
    return pd.Series(_cs_zscore(x.to_numpy()), index=x.index)


def cs_demean(x: pd.Series) -> pd.Series:
    return pd.Series(_cs_demean(x.to_numpy()), index=x.index)


def cs_winsorize(x: pd.Series, k: float = 3.0) -> pd.Series:
    return pd.Series(_cs_winsorize(x.to_numpy(), k), index=x.index)


def cs_scale(x: pd.Series, a: float = 1.0) -> pd.Series:
    return pd.Series(_cs_scale(x.to_numpy(), a), index=x.index)


def rank(x: pd.Series) -> pd.Series:
    return pd.Series(_cs_rank(x.to_numpy()), index=x.index)
```

- [ ] **Step 2: Write `indicators/pandas/v5.py`**

Copy the batch functions from `alphas/base/v5_indicators.py`:

```python
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
```

- [ ] **Step 3: Write `indicators/pandas/technical.py`**

```python
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(x: pd.Series, d: int) -> pd.Series:
    return x.rolling(d, min_periods=d).mean()


def atr(high: pd.Series, low: pd.Series, close: pd.Series, d: int) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(d, min_periods=d).mean()


def bollinger_bands(close: pd.Series, d: int, k: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = sma(close, d)
    dev = close.rolling(d, min_periods=d).std()
    upper = mid + k * dev
    lower = mid - k * dev
    return upper, mid, lower
```

- [ ] **Step 4: Write tests for cs_ops**

```python
import numpy as np
import pandas as pd
from indicators.pandas.cs_ops import cs_zscore, cs_demean, cs_winsorize, cs_scale, rank


def test_cs_zscore_pandas():
    x = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    z = cs_zscore(x)
    assert abs(z.mean()) < 1e-10


def test_rank_pandas():
    x = pd.Series([30.0, 10.0, 20.0])
    r = rank(x)
    assert abs(r.iloc[0] - 1.0) < 1e-10
    assert abs(r.iloc[1] - 1 / 3) < 1e-10
    assert abs(r.iloc[2] - 2 / 3) < 1e-10
```

- [ ] **Step 5: Write tests for v5 batch**

```python
from indicators.pandas.v5 import compute_v5_tail_indicators


def test_v5_batch_insufficient_data():
    result = compute_v5_tail_indicators([1.0, 2.0], [1.5, 2.5], [0.5, 1.5], sma_len=50, atr_len=200, poc_len=30, norm_window=100)
    assert result is None


def test_v5_batch_sufficient_data():
    import random
    random.seed(42)
    closes = [100.0 + random.gauss(0, 1) for _ in range(350)]
    highs = [c + abs(random.gauss(0, 0.5)) for c in closes]
    lows = [c - abs(random.gauss(0, 0.5)) for c in closes]
    result = compute_v5_tail_indicators(closes, highs, lows, sma_len=50, atr_len=200, poc_len=30, norm_window=100)
    assert result is not None
    assert "acol" in result
    assert "atr" in result
    assert "poc" in result
```

- [ ] **Step 6: Write tests for technical**

```python
import numpy as np
import pandas as pd
from indicators.pandas.technical import sma, atr, bollinger_bands


def test_sma():
    x = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    result = sma(x, 3)
    assert abs(result.iloc[4] - 4.0) < 1e-10


def test_atr():
    high = pd.Series([105.0, 108.0, 107.0, 110.0, 109.0])
    low = pd.Series([100.0, 103.0, 102.0, 105.0, 104.0])
    close = pd.Series([102.0, 106.0, 104.0, 108.0, 107.0])
    result = atr(high, low, close, 3)
    assert not np.isnan(result.iloc[4])


def test_bollinger_bands():
    close = pd.Series(list(range(1, 21)), dtype=float)
    upper, mid, lower = bollinger_bands(close, 10)
    assert not np.isnan(upper.iloc[-1])
    assert (upper.iloc[-1] > mid.iloc[-1] > lower.iloc[-1])
```

- [ ] **Step 7: Write cross-validation test for V5 streaming vs batch**

Create `indicators/tests/test_streaming_v5.py`:

```python
import random
from indicators.streaming.v5 import V5SymbolState
from indicators.pandas.v5 import compute_v5_tail_indicators


def test_v5_streaming_matches_batch():
    random.seed(42)
    sma_len, atr_len, poc_len, norm_window = 50, 200, 30, 100
    closes = [100.0 + random.gauss(0, 1) for _ in range(350)]
    highs = [c + abs(random.gauss(0, 0.5)) for c in closes]
    lows = [c - abs(random.gauss(0, 0.5)) for c in closes]

    batch = compute_v5_tail_indicators(
        closes, highs, lows,
        sma_len=sma_len, atr_len=atr_len, poc_len=poc_len, norm_window=norm_window,
    )

    state = V5SymbolState(sma_len=sma_len, atr_len=atr_len, poc_len=poc_len, norm_window=norm_window)
    streaming_result = None
    for c, h, l in zip(closes, highs, lows):
        r = state.append(c, h, l)
        if r is not None:
            streaming_result = r

    assert batch is not None
    assert streaming_result is not None
    assert abs(streaming_result["acol"] - batch["acol"]) < 1e-6
    assert abs(streaming_result["atr"] - batch["atr"]) < 1e-4
    assert abs(streaming_result["poc"] - batch["poc"]) < 1e-4
```

- [ ] **Step 8: Run all tests**

Run: `python -m pytest indicators/tests/ -v`
Expected: All PASS

- [ ] **Step 9: Commit**

```bash
git add indicators/pandas/cs_ops.py indicators/pandas/v5.py indicators/pandas/technical.py indicators/tests/
git commit -m "feat: add pandas wrappers (cs_ops, v5 batch, technical) and V5 cross-validation test"
```

---

### Task 11: Verify all imports and run full test suite

**Files:** None (verification only)

- [ ] **Step 1: Verify top-level import works**

Run: `python -c "from indicators import EMA, RollingMoments, ts_mean, ts_zscore; print('OK')"`
Expected: `OK`

- [ ] **Step 2: Verify streaming subpackage import**

Run: `python -c "from indicators.streaming import V5SymbolState, cs_rank; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Verify pandas subpackage import**

Run: `python -c "from indicators.pandas import compute_v5_tail_indicators, bollinger_bands, rank; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Run full indicator test suite**

Run: `python -m pytest indicators/tests/ -v`
Expected: All PASS

- [ ] **Step 5: Run existing paper-trade-system tests to verify no regressions**

Run: `python -m pytest alphas/runner/tests/ -v --timeout=60`
Expected: All PASS (existing code unchanged)

- [ ] **Step 6: Commit if any fixes needed**

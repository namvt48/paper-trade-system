# Indicators Library for Paper-Trade-System

## Problem

Indicator/operator code is scattered across the codebase:
- `signal_engine.py` — streaming O(1)/bar operators, not imported anywhere
- `cross_alpha/strategy.py` — pandas-based ts/cs operators, used by 18 cross-sectional alphas
- `base/v5_indicators.py` — V5 tail indicators (SMA, ATR, median, acol, poc)
- 15+ alpha store files — copy-pasted `_calc_sma`, `_calc_atr`, `_calc_median`

No shared library exists. Adding a new indicator means duplicating code or creating yet another inline implementation.

## Goal

Create a unified indicators library at `paper-trade-system/indicators/` with:
1. Streaming core (stateful, O(1)/bar) — ported from `signal_engine.py` and `v5_indicators.py`
2. Pandas wrappers (stateless, batch) — ported from `cross_alpha/strategy.py` and alpha stores
3. Gradual migration path — alpha mới imports from lib, alpha cũ giữ nguyên

## Directory Structure

```
paper-trade-system/indicators/
  __init__.py              # Re-export phổ biến
  streaming/
    __init__.py            # Re-export tất cả streaming classes
    moments.py             # RollingMoments
    ema.py                 # EMA
    decay.py               # DecayLinear
    extreme.py             # RollingExtreme (ts_min, ts_max)
    momentum.py            # Momentum
    cross_sectional.py     # cs_zscore, cs_demean, cs_winsorize, cs_scale, cs_rank
    v5.py                  # V5SymbolState
  pandas/
    __init__.py            # Re-export tất cả pandas functions
    ts_ops.py              # ts_mean, ts_std, ts_zscore, ts_skew, ts_momentum, decay_linear
    cs_ops.py              # cs_zscore, cs_demean, cs_winsorize, cs_scale, rank
    v5.py                  # compute_v5_tail_indicators (batch)
    technical.py           # sma, atr, bollinger_bands, rsi
```

## Streaming Core

Ported from `signal_engine.py` — no logic changes, only file splits.

### streaming/moments.py — RollingMoments

```python
class RollingMoments:
    """Window mean/std(ddof=1)/zscore/skew via running power sums."""
    __slots__ = ("d", "buf", "s1", "s2", "s3")
    def __init__(self, d): ...
    def update(self, x) -> self: ...   # NaN doesn't advance window
    def mean(self) -> float: ...
    def std(self) -> float: ...         # ddof=1
    def zscore(self, x) -> float: ...
    def skew(self) -> float: ...        # population moment m3/m2^1.5
```

### streaming/ema.py — EMA

```python
class EMA:
    __slots__ = ("a", "y", "n", "minp")
    def __init__(self, span): ...       # alpha = 2/(span+1), minp = span//2
    def update(self, x) -> self: ...    # NaN skipped
    def value(self) -> float: ...       # nan if n < minp
```

### streaming/decay.py — DecayLinear

```python
class DecayLinear:
    """Linearly-weighted MA, newest weight d..oldest 1. O(1): WS = WS - S + d*x."""
    __slots__ = ("d", "buf", "S", "WS", "norm")
    def __init__(self, d): ...
    def update(self, x) -> self: ...    # NaN skipped
    def value(self) -> float: ...       # nan if window not full
```

### streaming/extreme.py — RollingExtreme

```python
class RollingExtreme:
    """ts_min/ts_max in O(1) amortised via monotonic deque."""
    __slots__ = ("d", "dq", "t", "is_max")
    def __init__(self, d, is_max=True): ...
    def update(self, x) -> self: ...    # NaN skipped
    def value(self) -> float: ...       # nan if t < d
```

### streaming/momentum.py — Momentum

```python
class Momentum:
    """ts_momentum(x, d) = x_t / x_{t-d} - 1."""
    __slots__ = ("d", "buf")
    def __init__(self, d): ...
    def update(self, x) -> self: ...    # NaN skipped
    def value(self) -> float: ...
```

### streaming/cross_sectional.py

```python
def cs_zscore(v: np.ndarray) -> np.ndarray: ...
def cs_demean(v: np.ndarray) -> np.ndarray: ...
def cs_winsorize(v: np.ndarray, k=3.0) -> np.ndarray: ...
def cs_scale(v: np.ndarray, a=1.0) -> np.ndarray: ...
def cs_rank(v: np.ndarray) -> np.ndarray: ...
```

All take/return numpy arrays. No pandas dependency.

### streaming/v5.py — V5SymbolState

```python
class V5SymbolState:
    """Incremental streaming version of v5_indicators: acol, atr, poc."""
    def update(self, open, high, low, close, volume) -> None: ...
    @property
    def acol(self) -> float: ...
    @property
    def atr(self) -> float: ...
    @property
    def poc(self) -> float: ...
```

Ported from `base/v5_indicators.py` `V5SymbolState` class.

## Pandas Wrappers

Batch functions. Input: `pd.Series` or `np.ndarray`. Output: `pd.Series`.

### pandas/ts_ops.py

```python
def ts_mean(x: pd.Series, d: int) -> pd.Series: ...      # x.rolling(d).mean()
def ts_std(x: pd.Series, d: int) -> pd.Series: ...       # x.rolling(d).std()
def ts_zscore(x: pd.Series, d: int) -> pd.Series: ...    # (x - ts_mean) / ts_std
def ts_skew(x: pd.Series, d: int) -> pd.Series: ...      # x.rolling(d).skew()
def ts_momentum(x: pd.Series, d: int) -> pd.Series: ...  # x / x.shift(d) - 1
def decay_linear(x: pd.Series, d: int) -> pd.Series: ... # weighted MA, linear weights
```

Ported from `cross_alpha/strategy.py` lines 176-200.

### pandas/cs_ops.py

```python
def cs_zscore(x: pd.Series) -> pd.Series: ...
def cs_demean(x: pd.Series) -> pd.Series: ...
def cs_winsorize(x: pd.Series, k=3.0) -> pd.Series: ...
def cs_scale(x: pd.Series, a=1.0) -> pd.Series: ...
def rank(x: pd.Series) -> pd.Series: ...
```

Wrappers around streaming core `cross_sectional.py` functions (convert pd.Series → np.ndarray → call core → convert back).

### pandas/v5.py

```python
def compute_v5_tail_indicators(closes, highs, lows, opens=None, volumes=None, ...) -> dict: ...
```

Ported from `base/v5_indicators.py` `compute_v5_tail_indicators()`.

### pandas/technical.py

```python
def sma(x: pd.Series, d: int) -> pd.Series: ...
def atr(high: pd.Series, low: pd.Series, close: pd.Series, d: int) -> pd.Series: ...
def bollinger_bands(close: pd.Series, d: int, k: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]: ...
def rsi(close: pd.Series, d: int) -> pd.Series: ...
```

Ported from inline code in alpha stores (`_calc_sma`, `_calc_atr`, hyper-turbo bollinger).

## `__init__.py` Exports

### indicators/__init__.py
```python
from indicators.streaming import EMA, RollingMoments, DecayLinear, RollingExtreme, Momentum
from indicators.pandas import ts_mean, ts_std, ts_zscore, ts_skew, ts_momentum, decay_linear
```

### indicators/streaming/__init__.py
```python
from indicators.streaming.moments import RollingMoments
from indicators.streaming.ema import EMA
from indicators.streaming.decay import DecayLinear
from indicators.streaming.extreme import RollingExtreme
from indicators.streaming.momentum import Momentum
from indicators.streaming.cross_sectional import cs_zscore, cs_demean, cs_winsorize, cs_scale, cs_rank
from indicators.streaming.v5 import V5SymbolState
```

### indicators/pandas/__init__.py
```python
from indicators.pandas.ts_ops import ts_mean, ts_std, ts_zscore, ts_skew, ts_momentum, decay_linear
from indicators.pandas.cs_ops import cs_zscore, cs_demean, cs_winsorize, cs_scale, rank
from indicators.pandas.v5 import compute_v5_tail_indicators
from indicators.pandas.technical import sma, atr, bollinger_bands, rsi
```

## Gradual Migration

1. Create lib — no changes to existing code
2. New alphas import from `indicators.streaming` or `indicators.pandas`
3. Old alphas migrate one-by-one when convenient:
   - `from alphas.base.v5_indicators import V5SymbolState` → `from indicators.streaming.v5 import V5SymbolState`
   - Inline `_calc_sma`, `_calc_atr`, `_calc_median` → `from indicators.pandas.technical import sma, atr`
4. `cross_alpha/strategy.py` keeps its own implementations until lib is stable, then migrates

## Dependencies

- Streaming core: `numpy`, `collections.deque` only
- Pandas wrappers: `pandas`, `numpy`
- No new external libraries. No `pandas_ta`. Alphas needing pandas_ta import it directly.

## Testing

- Unit test each streaming class: `.update()` state correctness, `.value()` output, NaN handling, edge cases (window not full)
- Unit test each pandas function: compare output with direct `x.rolling(d).mean()` etc.
- Unit test V5 streaming vs batch: `V5SymbolState` incremental output matches `compute_v5_tail_indicators` batch output
- Cross-validation: pandas wrapper output ≈ streaming output for same input sequence

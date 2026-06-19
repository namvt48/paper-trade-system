from __future__ import annotations

import numpy as np
import pandas as pd


def ts_mean(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x.rolling(d, min_periods=max(1, d // 2)).mean()


def ts_std(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x.rolling(d, min_periods=max(2, d // 2)).std()


def ts_zscore(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return (x - ts_mean(x, d)) / ts_std(x, d).replace(0, np.nan)


def ts_skew(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x.rolling(d, min_periods=max(3, d // 2)).skew()


def ts_momentum(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x / x.shift(d) - 1.0


def decay_linear(x: pd.DataFrame, d: int) -> pd.DataFrame:
    weights = np.arange(d, 0, -1, dtype=float)
    weights /= weights.sum()
    return sum(weights[k] * x.shift(k) for k in range(d))

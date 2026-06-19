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

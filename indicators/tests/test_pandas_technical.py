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
    assert upper.iloc[-1] > mid.iloc[-1] > lower.iloc[-1]

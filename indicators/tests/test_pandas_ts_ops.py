import numpy as np
import pandas as pd
from indicators.pandas.ts_ops import ts_mean, ts_std, ts_zscore, ts_skew, ts_momentum, decay_linear, ts_range_location, ts_range_location_close


def test_ts_mean():
    x = pd.DataFrame({"A": [1.0, 2.0, 3.0, 4.0, 5.0]})
    result = ts_mean(x, 3)
    assert abs(result.iloc[4, 0] - 4.0) < 1e-10


def test_ts_std():
    x = pd.DataFrame({"A": [2.0, 4.0, 4.0, 4.0]})
    result = ts_std(x, 4)
    assert abs(result.iloc[3, 0] - 1.0) < 1e-10


def test_ts_zscore():
    x = pd.DataFrame({"A": list(range(1, 21))}, dtype=float)
    result = ts_zscore(x, 10)
    assert not np.isnan(result.iloc[-1, 0])


def test_ts_momentum():
    x = pd.DataFrame({"A": [100.0, 110.0, 121.0]})
    result = ts_momentum(x, 1)
    assert abs(result.iloc[2, 0] - 0.1) < 1e-10


def test_decay_linear():
    x = pd.DataFrame({"A": [1.0, 2.0, 3.0]})
    result = decay_linear(x, 2)
    assert not np.isnan(result.iloc[2, 0])


def test_ts_range_location():
    close = pd.DataFrame({"A": [1.0, 2.0, 3.0, 4.0, 5.0]})
    low = pd.DataFrame({"A": [0.5, 1.5, 2.5, 3.5, 4.5]})
    high = pd.DataFrame({"A": [1.5, 2.5, 3.5, 4.5, 5.5]})
    result = ts_range_location(close, low, high, 3)
    last = result.iloc[4, 0]
    lo = low["A"].rolling(3, min_periods=1).min().iloc[4]
    hi = high["A"].rolling(3, min_periods=1).max().iloc[4]
    expected = (5.0 - lo) / (hi - lo)
    assert abs(last - expected) < 1e-10
    assert 0.0 <= last <= 1.0


def test_ts_range_location_at_high():
    close = pd.DataFrame({"A": [1.0, 2.0, 3.0, 4.0, 5.0]})
    low = pd.DataFrame({"A": [0.5, 1.0, 1.5, 2.0, 2.5]})
    high = pd.DataFrame({"A": [1.5, 2.0, 2.5, 3.0, 5.0]})
    result = ts_range_location(close, low, high, 5)
    assert abs(result.iloc[4, 0] - 1.0) < 1e-10


def test_ts_range_location_close():
    close = pd.DataFrame({"A": [1.0, 2.0, 3.0, 2.0, 5.0]})
    result = ts_range_location_close(close, 5)
    lo = 1.0
    hi = 5.0
    expected = (5.0 - lo) / (hi - lo)
    assert abs(result.iloc[4, 0] - expected) < 1e-10
    assert 0.0 <= result.iloc[4, 0] <= 1.0


def test_ts_range_location_close_at_low():
    close = pd.DataFrame({"A": [5.0, 4.0, 3.0, 2.0, 1.0]})
    result = ts_range_location_close(close, 5)
    assert abs(result.iloc[4, 0] - 0.0) < 1e-10

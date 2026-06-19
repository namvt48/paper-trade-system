import numpy as np
import pandas as pd
from indicators.pandas.ts_ops import ts_mean, ts_std, ts_zscore, ts_skew, ts_momentum, decay_linear


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

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

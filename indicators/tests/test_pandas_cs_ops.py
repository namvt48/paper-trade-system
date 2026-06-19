import numpy as np
import pandas as pd
from indicators.pandas.cs_ops import cs_zscore, cs_demean, cs_winsorize, cs_scale, rank


def test_cs_zscore_dataframe():
    x = pd.DataFrame({"A": [1.0, 2.0, 3.0], "B": [4.0, 5.0, 6.0], "C": [7.0, 8.0, 9.0]})
    z = cs_zscore(x)
    assert abs(z.mean(axis=1).iloc[0]) < 1e-10


def test_cs_demean_dataframe():
    x = pd.DataFrame({"A": [1.0, 2.0], "B": [3.0, 4.0]})
    d = cs_demean(x)
    assert abs(d.mean(axis=1).iloc[0]) < 1e-10


def test_cs_winsorize_dataframe():
    x = pd.DataFrame({"A": [1.0], "B": [2.0], "C": [100.0]})
    w = cs_winsorize(x, k=1.0)
    assert w.iloc[0].max() < 100.0


def test_cs_scale_dataframe():
    x = pd.DataFrame({"A": [3.0], "B": [4.0]})
    s = cs_scale(x, a=1.0)
    assert abs(s.abs().sum(axis=1).iloc[0] - 1.0) < 1e-10


def test_rank_dataframe():
    x = pd.DataFrame({"A": [30.0], "B": [10.0], "C": [20.0]})
    r = rank(x)
    assert abs(r.iloc[0, 0] - 1.0) < 1e-10
    assert abs(r.iloc[0, 1] - 1 / 3) < 1e-10
    assert abs(r.iloc[0, 2] - 2 / 3) < 1e-10

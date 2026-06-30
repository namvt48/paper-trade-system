import numpy as np
import pandas as pd
from indicators.pandas.element_ops import abs, neg, add, div


def test_abs():
    x = pd.DataFrame({"A": [-1.0, 0.0, 2.0, -3.0]})
    result = abs(x)
    assert list(result["A"]) == [1.0, 0.0, 2.0, 3.0]


def test_neg():
    x = pd.DataFrame({"A": [1.0, -2.0, 0.0]})
    result = neg(x)
    assert list(result["A"]) == [-1.0, 2.0, 0.0]


def test_add():
    left = pd.DataFrame({"A": [1.0, 2.0, 3.0]})
    right = pd.DataFrame({"A": [10.0, 20.0, 30.0]})
    result = add(left, right)
    assert list(result["A"]) == [11.0, 22.0, 33.0]


def test_div():
    left = pd.DataFrame({"A": [10.0, 20.0, 30.0]})
    right = pd.DataFrame({"A": [2.0, 5.0, 10.0]})
    result = div(left, right)
    assert list(result["A"]) == [5.0, 4.0, 3.0]


def test_div_by_zero_returns_inf():
    left = pd.DataFrame({"A": [1.0, 2.0]})
    right = pd.DataFrame({"A": [0.0, 2.0]})
    result = div(left, right)
    assert np.isinf(result.iloc[0, 0])
    assert result.iloc[1, 0] == 1.0

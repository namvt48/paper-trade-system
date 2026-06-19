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
    w = cs_winsorize(v, k=1.0)
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

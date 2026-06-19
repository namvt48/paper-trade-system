import math
from indicators.streaming.moments import RollingMoments


def test_mean_full_window():
    rm = RollingMoments(3)
    rm.update(2.0); rm.update(4.0); rm.update(6.0)
    assert rm.mean() == 4.0


def test_mean_partial_window():
    rm = RollingMoments(5)
    rm.update(2.0); rm.update(4.0)
    assert rm.mean() == 3.0


def test_mean_sliding_window():
    rm = RollingMoments(3)
    rm.update(1.0); rm.update(2.0); rm.update(3.0); rm.update(4.0)
    assert rm.mean() == 3.0


def test_std_ddof1():
    rm = RollingMoments(4)
    rm.update(2.0); rm.update(4.0); rm.update(4.0); rm.update(4.0)
    assert abs(rm.std() - 1.0) < 1e-10


def test_std_less_than_two_returns_nan():
    rm = RollingMoments(5)
    rm.update(1.0)
    assert math.isnan(rm.std())


def test_zscore():
    rm = RollingMoments(4)
    rm.update(2.0); rm.update(4.0); rm.update(4.0); rm.update(4.0)
    z = rm.zscore(3.0)
    assert abs(z - (-0.5)) < 1e-10


def test_skew():
    rm = RollingMoments(4)
    rm.update(1.0); rm.update(2.0); rm.update(5.0); rm.update(8.0)
    s = rm.skew()
    assert not math.isnan(s)


def test_skew_less_than_three_returns_nan():
    rm = RollingMoments(5)
    rm.update(1.0); rm.update(2.0)
    assert math.isnan(rm.skew())


def test_nan_does_not_advance_window():
    rm = RollingMoments(3)
    rm.update(1.0); rm.update(float("nan")); rm.update(3.0)
    assert rm.mean() == 2.0


def test_update_returns_self():
    rm = RollingMoments(3)
    assert rm.update(1.0) is rm


def test_empty_mean_is_nan():
    rm = RollingMoments(3)
    assert math.isnan(rm.mean())

import math
from indicators.streaming.momentum import Momentum


def test_momentum_not_enough_data():
    mom = Momentum(3)
    mom.update(100.0); mom.update(110.0)
    assert math.isnan(mom.value())


def test_momentum_basic():
    mom = Momentum(2)
    mom.update(100.0); mom.update(110.0); mom.update(121.0)
    assert abs(mom.value() - 0.21) < 1e-10


def test_momentum_zero_base():
    mom = Momentum(1)
    mom.update(0.0); mom.update(5.0)
    assert math.isnan(mom.value())


def test_nan_skipped():
    mom = Momentum(2)
    mom.update(100.0); mom.update(float("nan")); mom.update(110.0)
    assert math.isnan(mom.value())


def test_update_returns_self():
    mom = Momentum(3)
    assert mom.update(1.0) is mom

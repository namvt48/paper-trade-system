import math
from indicators.streaming.decay import DecayLinear


def test_decay_linear_not_full_returns_nan():
    dl = DecayLinear(3)
    dl.update(1.0)
    assert math.isnan(dl.value())


def test_decay_linear_full_window():
    dl = DecayLinear(3)
    dl.update(1.0); dl.update(2.0); dl.update(3.0)
    assert abs(dl.value() - 14.0 / 6.0) < 1e-10


def test_decay_linear_sliding():
    dl = DecayLinear(3)
    dl.update(1.0); dl.update(2.0); dl.update(3.0); dl.update(4.0)
    assert abs(dl.value() - 20.0 / 6.0) < 1e-10


def test_decay_linear_nan_skipped():
    dl = DecayLinear(3)
    dl.update(1.0); dl.update(float("nan")); dl.update(3.0)
    assert math.isnan(dl.value())


def test_decay_linear_update_returns_self():
    dl = DecayLinear(3)
    assert dl.update(1.0) is dl

import math
from indicators.streaming.ema import EMA


def test_ema_converges():
    ema = EMA(10)
    for _ in range(20):
        ema.update(100.0)
    assert abs(ema.value() - 100.0) < 1e-10


def test_ema_min_periods():
    ema = EMA(10)
    ema.update(100.0)
    assert math.isnan(ema.value())
    for _ in range(4):
        ema.update(100.0)
    assert not math.isnan(ema.value())


def test_ema_nan_skipped():
    ema = EMA(10)
    for _ in range(5):
        ema.update(100.0)
    ema.update(float("nan"))
    assert abs(ema.value() - 100.0) < 1e-10


def test_ema_update_returns_self():
    ema = EMA(10)
    assert ema.update(1.0) is ema

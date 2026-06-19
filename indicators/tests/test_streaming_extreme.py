import math
from indicators.streaming.extreme import RollingExtreme


def test_rolling_max():
    re = RollingExtreme(3, is_max=True)
    re.update(1.0); re.update(3.0); re.update(2.0)
    assert re.value() == 3.0


def test_rolling_max_slides():
    re = RollingExtreme(3, is_max=True)
    re.update(1.0); re.update(3.0); re.update(2.0); re.update(1.0)
    assert re.value() == 3.0


def test_rolling_max_expires():
    re = RollingExtreme(3, is_max=True)
    re.update(1.0); re.update(3.0); re.update(2.0); re.update(1.0); re.update(0.5)
    assert re.value() == 2.0


def test_rolling_min():
    re = RollingExtreme(3, is_max=False)
    re.update(3.0); re.update(1.0); re.update(2.0)
    assert re.value() == 1.0


def test_not_enough_data_returns_nan():
    re = RollingExtreme(3)
    re.update(1.0); re.update(2.0)
    assert math.isnan(re.value())


def test_nan_skipped():
    re = RollingExtreme(3)
    re.update(1.0); re.update(float("nan")); re.update(3.0)
    assert math.isnan(re.value())


def test_update_returns_self():
    re = RollingExtreme(3)
    assert re.update(1.0) is re

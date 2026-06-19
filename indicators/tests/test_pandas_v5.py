import random
from indicators.pandas.v5 import compute_v5_tail_indicators


def test_v5_batch_insufficient_data():
    result = compute_v5_tail_indicators([1.0, 2.0], [1.5, 2.5], [0.5, 1.5], sma_len=50, atr_len=200, poc_len=30, norm_window=100)
    assert result is None


def test_v5_batch_sufficient_data():
    random.seed(42)
    closes = [100.0 + random.gauss(0, 1) for _ in range(350)]
    highs = [c + abs(random.gauss(0, 0.5)) for c in closes]
    lows = [c - abs(random.gauss(0, 0.5)) for c in closes]
    result = compute_v5_tail_indicators(closes, highs, lows, sma_len=50, atr_len=200, poc_len=30, norm_window=100)
    assert result is not None
    assert "acol" in result
    assert "atr" in result
    assert "poc" in result

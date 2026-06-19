import random
from indicators.streaming.v5 import V5SymbolState
from indicators.pandas.v5 import compute_v5_tail_indicators


def test_v5_streaming_matches_batch():
    random.seed(42)
    sma_len, atr_len, poc_len, norm_window = 50, 200, 30, 100
    closes = [100.0 + random.gauss(0, 1) for _ in range(350)]
    highs = [c + abs(random.gauss(0, 0.5)) for c in closes]
    lows = [c - abs(random.gauss(0, 0.5)) for c in closes]

    batch = compute_v5_tail_indicators(
        closes, highs, lows,
        sma_len=sma_len, atr_len=atr_len, poc_len=poc_len, norm_window=norm_window,
    )

    state = V5SymbolState(sma_len=sma_len, atr_len=atr_len, poc_len=poc_len, norm_window=norm_window)
    streaming_result = None
    for c, h, l in zip(closes, highs, lows):
        r = state.append(c, h, l)
        if r is not None:
            streaming_result = r

    assert batch is not None
    assert streaming_result is not None
    assert abs(streaming_result["acol"] - batch["acol"]) < 1e-6
    assert abs(streaming_result["atr"] - batch["atr"]) < 1e-4
    assert abs(streaming_result["poc"] - batch["poc"]) < 1e-4

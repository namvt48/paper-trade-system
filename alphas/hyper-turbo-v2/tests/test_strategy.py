import pytest

from app.strategy import H4_MS, compute_hyper_turbo_signal


def make_bars(last_close: float, last_range: float = 20.0, bars: int = 309):
    closes = [100.0] * (bars - 1) + [last_close]
    highs = [101.0] * bars
    lows = [99.0] * bars
    highs[-1] = last_close + last_range / 2
    lows[-1] = last_close - last_range / 2
    times = [idx * H4_MS for idx in range(bars)]
    return closes, highs, lows, times


def test_ensemble_long_passes_htf_and_atr_rising_gates():
    signal = compute_hyper_turbo_signal(*make_bars(112.0))

    assert signal is not None
    assert signal.recommend == "LONG"
    assert signal.go_long is True
    assert signal.period_votes == (1, 1, 1)
    assert signal.atr_rising is True
    assert signal.htf_pass is True
    assert signal.htf_ma == pytest.approx(100.0)


def test_ensemble_short_passes_htf_and_atr_rising_gates():
    signal = compute_hyper_turbo_signal(*make_bars(88.0))

    assert signal is not None
    assert signal.recommend == "SHORT"
    assert signal.go_short is True
    assert signal.period_votes == (-1, -1, -1)


def test_atr_gate_blocks_flat_volatility_breakout():
    signal = compute_hyper_turbo_signal(*make_bars(101.0, last_range=0.0))

    assert signal is not None
    assert signal.period_votes == (1, 1, 1)
    assert signal.atr_rising is False
    assert signal.go_long is True
    assert signal.recommend is None


def test_daily_ma_uses_only_days_completed_at_signal_close():
    closes, highs, lows, times = make_bars(112.0)
    # A spike earlier in the still-open signal day must not enter D1 MA50.
    closes[-2] = 1_000.0
    highs[-2] = 1_001.0
    lows[-2] = 999.0

    signal = compute_hyper_turbo_signal(closes, highs, lows, times)

    assert signal is not None
    assert signal.htf_ma == pytest.approx(100.0)

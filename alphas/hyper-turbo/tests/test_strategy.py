from app.strategy import compute_hyper_turbo_signal


def test_detects_long_and_short_trend_crossovers():
    long_signal = compute_hyper_turbo_signal([10.0, 10.0, 10.0, 12.0], 3, 2.5)
    short_signal = compute_hyper_turbo_signal([10.0, 10.0, 10.0, 8.0], 3, 2.5)

    assert long_signal.recommend == "LONG"
    assert long_signal.go_long is True
    assert short_signal.recommend == "SHORT"
    assert short_signal.go_short is True


def test_detects_tp_crosses_from_source_rules():
    tp_long = compute_hyper_turbo_signal([10.0] * 19 + [30.0, 10.0], 20, 2.5)
    tp_short = compute_hyper_turbo_signal([10.0] * 19 + [-10.0, 10.0], 20, 2.5)

    assert tp_long.tp_long_signal is True
    assert tp_short.tp_short_signal is True

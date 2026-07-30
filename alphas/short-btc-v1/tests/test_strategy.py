from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ALPHA_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ALPHA_DIR))

strategy = importlib.import_module("app.strategy")


def _build_downtrend_series(n: int = 30):
    closes = [100.0 - i for i in range(n)]
    opens = [c + 0.3 for c in closes]
    highs = [c + 1.0 for c in closes]
    lows = [c - 0.1 for c in closes]
    return closes, highs, lows, opens


# ── calc_ema / calc_rsi / calc_atr / calc_clv ──────────────────────────────


def test_calc_ema_returns_none_before_warmup_then_simple_average():
    out = strategy.calc_ema([1.0, 2.0, 3.0, 4.0, 5.0], 3)
    assert out[0] is None and out[1] is None
    assert out[2] == pytest.approx(2.0)


def test_calc_rsi_pure_uptrend_is_100():
    vals = [float(i) for i in range(1, 20)]
    out = strategy.calc_rsi(vals, 5)
    assert out[5] == pytest.approx(100.0)


def test_calc_rsi_pure_downtrend_is_0():
    vals = [float(-i) for i in range(1, 20)]
    out = strategy.calc_rsi(vals, 5)
    assert out[5] == pytest.approx(0.0)


def test_calc_atr_none_before_warmup_then_positive():
    highs = [i + 1.0 for i in range(10)]
    lows = [i - 1.0 for i in range(10)]
    closes = [float(i) for i in range(10)]
    out = strategy.calc_atr(highs, lows, closes, 5)
    assert out[4] is None
    assert out[5] is not None and out[5] > 0


def test_calc_clv_bounds():
    assert strategy.calc_clv(high=10.0, low=10.0, close=10.0) == 0.0
    assert strategy.calc_clv(high=10.0, low=0.0, close=10.0) == 1.0
    assert strategy.calc_clv(high=10.0, low=0.0, close=0.0) == 0.0


# ── passes_d1_downtrend ─────────────────────────────────────────────────────


def test_passes_d1_downtrend_true_on_sustained_decline():
    n = 20
    closes = [100.0 - i for i in range(n)]
    open_times = [i * 86_400_000 for i in range(n)]
    ema_fast = strategy.calc_ema(closes, 3)
    ema_slow = strategy.calc_ema(closes, 5)
    signal_time_ms = open_times[-1] + 86_400_000 + 1

    assert strategy.passes_d1_downtrend(
        closes, ema_fast, ema_slow, open_times, signal_time_ms, slope_lookback=5,
    ) is True


def test_passes_d1_downtrend_false_on_uptrend():
    n = 20
    closes = [100.0 + i for i in range(n)]
    open_times = [i * 86_400_000 for i in range(n)]
    ema_fast = strategy.calc_ema(closes, 3)
    ema_slow = strategy.calc_ema(closes, 5)
    signal_time_ms = open_times[-1] + 86_400_000 + 1

    assert strategy.passes_d1_downtrend(
        closes, ema_fast, ema_slow, open_times, signal_time_ms, slope_lookback=5,
    ) is False


def test_passes_d1_downtrend_false_when_no_completed_bar_yet():
    assert strategy.passes_d1_downtrend(
        [100.0], [None], [None], [0], signal_time_ms=100, slope_lookback=5,
    ) is False


# ── compute_entry_signal ─────────────────────────────────────────────────────


def test_compute_entry_signal_fires_on_downtrend_breakdown():
    closes, highs, lows, opens = _build_downtrend_series()
    ema_fast = strategy.calc_ema(closes, 3)
    ema_slow = strategy.calc_ema(closes, 5)
    rsi = strategy.calc_rsi(closes, 5)
    atr = strategy.calc_atr(highs, lows, closes, 5)

    signal = strategy.compute_entry_signal(
        closes, highs, lows, opens, ema_fast, ema_slow, rsi, atr,
        lookback_bars=5, rsi_thresh=40.0, clv_max=0.25, sl_atr_mult=0.8, tp_ratio=1.2,
    )

    assert signal is not None
    assert signal["entry"] == closes[-1]
    assert signal["sl"] > signal["entry"]
    assert signal["tp"] < signal["entry"]


def test_compute_entry_signal_none_when_clv_too_tight():
    closes, highs, lows, opens = _build_downtrend_series()
    ema_fast = strategy.calc_ema(closes, 3)
    ema_slow = strategy.calc_ema(closes, 5)
    rsi = strategy.calc_rsi(closes, 5)
    atr = strategy.calc_atr(highs, lows, closes, 5)

    signal = strategy.compute_entry_signal(
        closes, highs, lows, opens, ema_fast, ema_slow, rsi, atr,
        lookback_bars=5, rsi_thresh=40.0, clv_max=0.01, sl_atr_mult=0.8, tp_ratio=1.2,
    )
    assert signal is None


def test_compute_entry_signal_none_before_indicator_warmup():
    closes, highs, lows, opens = _build_downtrend_series(n=10)
    ema_fast = strategy.calc_ema(closes, 3)
    ema_slow = strategy.calc_ema(closes, 20)  # never warms up within 10 bars
    rsi = strategy.calc_rsi(closes, 5)
    atr = strategy.calc_atr(highs, lows, closes, 5)

    signal = strategy.compute_entry_signal(
        closes, highs, lows, opens, ema_fast, ema_slow, rsi, atr,
        lookback_bars=5, rsi_thresh=40.0, clv_max=0.25, sl_atr_mult=0.8, tp_ratio=1.2,
    )
    assert signal is None


# ── read_last_at_or_before / read_last_completed_daily ──────────────────────


def test_read_last_at_or_before():
    rows = [{"t": 100, "v": 1}, {"t": 200, "v": 2}, {"t": 300, "v": 3}]
    assert strategy.read_last_at_or_before(rows, "t", 250)["v"] == 2
    assert strategy.read_last_at_or_before(rows, "t", 50) is None
    assert strategy.read_last_at_or_before(rows, "t", 300)["v"] == 3


def test_read_last_completed_daily_returns_row_and_previous():
    day_ms = 86_400_000
    rows = [
        {"t": 0, "oi_close": 10},
        {"t": day_ms, "oi_close": 20},
        {"t": 2 * day_ms, "oi_close": 30},
    ]

    row, prev = strategy.read_last_completed_daily(rows, "t", ts_ms=3 * day_ms + 1)
    assert row["oi_close"] == 30
    assert prev["oi_close"] == 20

    row, prev = strategy.read_last_completed_daily(rows, "t", ts_ms=day_ms)
    assert row["oi_close"] == 10
    assert prev is None


def test_read_last_completed_daily_none_when_no_bucket_closed():
    day_ms = 86_400_000
    rows = [{"t": 0, "oi_close": 10}]
    row, prev = strategy.read_last_completed_daily(rows, "t", ts_ms=day_ms - 1)
    assert row is None
    assert prev is None


# ── compute_context_exit_fraction ────────────────────────────────────────────


def test_context_exit_fraction_bad_count_zero_is_half():
    frac, fields = strategy.compute_context_exit_fraction(
        funding_rate=0.001, oi_close=110.0, oi_prev_close=100.0,
    )
    assert frac == 0.5
    assert fields["context_bad_count"] == 0
    assert fields["oi_daily_change_pct"] == pytest.approx(10.0)


def test_context_exit_fraction_bad_count_one_is_0_7():
    frac, fields = strategy.compute_context_exit_fraction(
        funding_rate=-0.0001, oi_close=110.0, oi_prev_close=100.0,
    )
    assert frac == 0.7
    assert fields["context_bad_count"] == 1


def test_context_exit_fraction_bad_count_two_is_full():
    frac, fields = strategy.compute_context_exit_fraction(
        funding_rate=-0.0001, oi_close=90.0, oi_prev_close=100.0,
    )
    assert frac == 1.0
    assert fields["context_bad_count"] == 2


def test_context_exit_fraction_handles_missing_context_data():
    frac, fields = strategy.compute_context_exit_fraction(
        funding_rate=None, oi_close=None, oi_prev_close=None,
    )
    assert frac == 0.5
    assert fields["oi_daily_change_pct"] is None

import numpy as np
import pandas as pd
from indicators.pandas.ts_ops import (
    ts_mean, ts_std, ts_zscore, ts_skew, ts_momentum, decay_linear,
    ts_range_location, ts_range_location_close, ts_ema, kaufman_er, cmf, ts_vwap,
    ideal_amp,
)


def test_ts_mean():
    x = pd.DataFrame({"A": [1.0, 2.0, 3.0, 4.0, 5.0]})
    result = ts_mean(x, 3)
    assert abs(result.iloc[4, 0] - 4.0) < 1e-10


def test_ts_std():
    x = pd.DataFrame({"A": [2.0, 4.0, 4.0, 4.0]})
    result = ts_std(x, 4)
    assert abs(result.iloc[3, 0] - 1.0) < 1e-10


def test_ts_zscore():
    x = pd.DataFrame({"A": list(range(1, 21))}, dtype=float)
    result = ts_zscore(x, 10)
    assert not np.isnan(result.iloc[-1, 0])


def test_ts_momentum():
    x = pd.DataFrame({"A": [100.0, 110.0, 121.0]})
    result = ts_momentum(x, 1)
    assert abs(result.iloc[2, 0] - 0.1) < 1e-10


def test_decay_linear():
    x = pd.DataFrame({"A": [1.0, 2.0, 3.0]})
    result = decay_linear(x, 2)
    assert not np.isnan(result.iloc[2, 0])


def test_decay_linear_with_gaps():
    x = pd.DataFrame({"A": [1.0, np.nan, 3.0, np.nan, np.nan, 6.0, 7.0, 8.0]})
    result = decay_linear(x, 4)
    assert not np.isnan(result.iloc[7, 0])


def test_decay_linear_all_nan():
    x = pd.DataFrame({"A": [np.nan, np.nan, np.nan, np.nan, np.nan]})
    result = decay_linear(x, 3)
    assert np.isnan(result.iloc[4, 0])


def test_ts_range_location():
    close = pd.DataFrame({"A": [1.0, 2.0, 3.0, 4.0, 5.0]})
    low = pd.DataFrame({"A": [0.5, 1.5, 2.5, 3.5, 4.5]})
    high = pd.DataFrame({"A": [1.5, 2.5, 3.5, 4.5, 5.5]})
    result = ts_range_location(close, low, high, 3)
    last = result.iloc[4, 0]
    lo = low["A"].rolling(3, min_periods=1).min().iloc[4]
    hi = high["A"].rolling(3, min_periods=1).max().iloc[4]
    expected = (5.0 - lo) / (hi - lo)
    assert abs(last - expected) < 1e-10
    assert 0.0 <= last <= 1.0


def test_ts_range_location_at_high():
    close = pd.DataFrame({"A": [1.0, 2.0, 3.0, 4.0, 5.0]})
    low = pd.DataFrame({"A": [0.5, 1.0, 1.5, 2.0, 2.5]})
    high = pd.DataFrame({"A": [1.5, 2.0, 2.5, 3.0, 5.0]})
    result = ts_range_location(close, low, high, 5)
    assert abs(result.iloc[4, 0] - 1.0) < 1e-10


def test_ts_range_location_close():
    close = pd.DataFrame({"A": [1.0, 2.0, 3.0, 2.0, 5.0]})
    result = ts_range_location_close(close, 5)
    lo = 1.0
    hi = 5.0
    expected = (5.0 - lo) / (hi - lo)
    assert abs(result.iloc[4, 0] - expected) < 1e-10
    assert 0.0 <= result.iloc[4, 0] <= 1.0


def test_ts_range_location_close_at_low():
    close = pd.DataFrame({"A": [5.0, 4.0, 3.0, 2.0, 1.0]})
    result = ts_range_location_close(close, 5)
    assert abs(result.iloc[4, 0] - 0.0) < 1e-10


def test_ts_ema_constant_series():
    x = pd.DataFrame({"A": [5.0, 5.0, 5.0, 5.0, 5.0]})
    result = ts_ema(x, 3)
    assert abs(result.iloc[4, 0] - 5.0) < 1e-10


def test_ts_ema_span_one_is_passthrough():
    # adjust=False, span=1 -> alpha=1.0 -> ewm output equals the input series exactly.
    x = pd.DataFrame({"A": [1.0, 4.0, 2.0, 9.0]})
    result = ts_ema(x, 1)
    assert np.allclose(result["A"].to_numpy(), x["A"].to_numpy())


def test_kaufman_er_perfect_trend_is_one():
    x = pd.DataFrame({"A": [1.0, 2.0, 3.0, 4.0, 5.0]})
    result = kaufman_er(x, 4)
    assert abs(result.iloc[4, 0] - 1.0) < 1e-10


def test_kaufman_er_choppy_is_zero():
    x = pd.DataFrame({"A": [1.0, 3.0, 1.0, 3.0, 1.0]})
    result = kaufman_er(x, 4)
    assert abs(result.iloc[4, 0] - 0.0) < 1e-10


def test_cmf_balanced_flow_is_zero():
    high = pd.DataFrame({"A": [2.0, 2.0, 2.0]})
    low = pd.DataFrame({"A": [1.0, 1.0, 1.0]})
    close = pd.DataFrame({"A": [2.0, 1.0, 1.5]})  # at high, at low, mid -> mfm = 1, -1, 0
    volume = pd.DataFrame({"A": [10.0, 10.0, 10.0]})
    result = cmf(high, low, close, volume, 3)
    assert abs(result.iloc[2, 0] - 0.0) < 1e-10


def test_cmf_all_buying_pressure_is_one():
    high = pd.DataFrame({"A": [2.0, 2.0, 2.0]})
    low = pd.DataFrame({"A": [1.0, 1.0, 1.0]})
    close = pd.DataFrame({"A": [2.0, 2.0, 2.0]})  # always at the high -> mfm = 1 every bar
    volume = pd.DataFrame({"A": [10.0, 5.0, 20.0]})
    result = cmf(high, low, close, volume, 3)
    assert abs(result.iloc[2, 0] - 1.0) < 1e-10


def test_ts_vwap_dollar_volume_weighted():
    high = pd.DataFrame({"A": [10.0, 20.0]})
    low = pd.DataFrame({"A": [10.0, 20.0]})
    close = pd.DataFrame({"A": [10.0, 20.0]})
    volume = pd.DataFrame({"A": [1.0, 3.0]})
    result = ts_vwap(high, low, close, volume, 2)
    # typical price == close here (high==low==close); vwap = (10*1 + 20*3) / (1+3) = 17.5
    assert abs(result.iloc[1, 0] - 17.5) < 1e-10


def test_ideal_amp_splits_top_k_bottom_k_by_close_rank():
    # window=4 needs >= window+5=9 valid bars before it computes anything
    # (matches datacryp/_scripts/_build_derived_v4.py::build_amplitude()).
    # close is the sort key; amp is designed to co-move with close (amp=close/100,
    # well under the 300% clip) so top/bottom-by-close == top/bottom-by-amp.
    close = pd.DataFrame({"A": [float(i) for i in range(1, 10)]})  # 1..9
    low = pd.DataFrame({"A": [100.0] * 9})
    amp_target = close["A"] / 100.0
    high = pd.DataFrame({"A": (low["A"] * (1 + amp_target)).to_numpy()})

    result = ideal_amp(high, low, close, window=4)

    # last window = positions 5..8 (close 6,7,8,9 / amp .06,.07,.08,.09), k=1
    # -> high-close day's amp (.09) - low-close day's amp (.06) = .03
    assert abs(result.iloc[8, 0] - 0.03) < 1e-9
    # 9 valid bars total meets the window+5=9 floor, so every position from
    # window-1=3 onward gets a value; earlier positions stay NaN (warm-up).
    assert np.isnan(result.iloc[2, 0])
    assert not np.isnan(result.iloc[3, 0])


def test_ideal_amp_clips_amplitude_at_300_percent():
    close = pd.DataFrame({"A": [float(i) for i in range(1, 10)]})
    low = pd.DataFrame({"A": [1.0] * 9})
    high = pd.DataFrame({"A": [100.0] * 9})  # amp = 99 -> clipped to 3.0 for every bar

    result = ideal_amp(high, low, close, window=4)

    # every bar has identical (clipped) amp=3.0, so high-group mean == low-group mean == 3.0
    assert abs(result.iloc[8, 0] - 0.0) < 1e-9


def test_ideal_amp_nan_when_insufficient_valid_bars():
    close = pd.DataFrame({"A": [1.0, 2.0, 3.0, np.nan, 5.0]})
    high = pd.DataFrame({"A": [1.1, 2.1, 3.1, 4.1, 5.1]})
    low = pd.DataFrame({"A": [1.0, 2.0, 3.0, 4.0, 5.0]})

    result = ideal_amp(high, low, close, window=4)

    assert result["A"].isna().all()

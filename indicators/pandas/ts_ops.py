from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view


def ts_mean(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x.rolling(d, min_periods=max(1, d // 2)).mean()


def ts_std(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x.rolling(d, min_periods=max(2, d // 2)).std()


def ts_zscore(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return (x - ts_mean(x, d)) / ts_std(x, d).replace(0, np.nan)


def ts_skew(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x.rolling(d, min_periods=max(3, d // 2)).skew()


def ts_momentum(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x / x.shift(d) - 1.0


def decay_linear(x: pd.DataFrame, d: int) -> pd.DataFrame:
    weights = np.arange(d, 0, -1, dtype=float)
    weights /= weights.sum()
    # Forward-fill gaps so a single missing candle doesn't null the entire
    # weighted window.  This matches the min_periods approach used by ts_mean
    # and ts_std — a few missing bars in a 240-bar window should not produce
    # NaN; they should be carried forward from the last available value.
    filled = x.ffill()
    result = sum(weights[k] * filled.shift(k) for k in range(d))
    # Mask: require at least half the window to be originally non-NaN so we
    # don't fabricate values from a single stale bar.
    min_periods = max(1, d // 2)
    valid_count = x.rolling(d, min_periods=1).count()
    return result.where(valid_count >= min_periods)


def ts_range_location(close: pd.DataFrame, low: pd.DataFrame, high: pd.DataFrame, d: int) -> pd.DataFrame:
    lo = low.rolling(d, min_periods=1).min()
    hi = high.rolling(d, min_periods=1).max()
    return (close - lo) / (hi - lo).replace(0, np.nan)


def ts_range_location_close(close: pd.DataFrame, d: int) -> pd.DataFrame:
    lo = close.rolling(d, min_periods=1).min()
    hi = close.rolling(d, min_periods=1).max()
    return (close - lo) / (hi - lo).replace(0, np.nan)


def ts_ema(x: pd.DataFrame, span: int) -> pd.DataFrame:
    return x.ewm(span=span, min_periods=max(1, span // 2), adjust=False).mean()


def kaufman_er(x: pd.DataFrame, d: int) -> pd.DataFrame:
    """Kaufman Efficiency Ratio: net directional change over d bars divided by
    the sum of bar-to-bar absolute changes over the same window. 1.0 = a
    perfectly straight trend, 0.0 = fully choppy/mean-reverting."""
    change = (x - x.shift(d)).abs()
    volatility = x.diff().abs().rolling(d, min_periods=max(1, d // 2)).sum()
    return change / volatility.replace(0, np.nan)


def cmf(high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame, volume: pd.DataFrame, d: int) -> pd.DataFrame:
    """Chaikin Money Flow: rolling sum of money-flow-volume divided by rolling
    sum of volume over d bars. Positive = buying pressure, negative = selling."""
    money_flow_multiplier = ((close - low) - (high - close)) / (high - low).replace(0, np.nan)
    money_flow_volume = money_flow_multiplier * volume
    mfv_sum = money_flow_volume.rolling(d, min_periods=max(1, d // 2)).sum()
    volume_sum = volume.rolling(d, min_periods=max(1, d // 2)).sum()
    return mfv_sum / volume_sum.replace(0, np.nan)


def ts_vwap(high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame, volume: pd.DataFrame, d: int) -> pd.DataFrame:
    """Rolling dollar-volume-weighted average price over d bars, using
    typical price (H+L+C)/3 as the per-bar price."""
    typical = (high + low + close) / 3.0
    pv_sum = (typical * volume).rolling(d, min_periods=max(1, d // 2)).sum()
    v_sum = volume.rolling(d, min_periods=max(1, d // 2)).sum()
    return pv_sum / v_sum.replace(0, np.nan)


def _rolling_split_diff(values: np.ndarray, sortkey: np.ndarray, window: int, k_frac: float = 0.25) -> np.ndarray:
    """For each trailing window of length ``window``: mean(values on the
    top-k rows by sortkey) - mean(values on the bottom-k rows), k = int(k_frac
    * window) rows by COUNT (not a quantile threshold). Aligned to the right
    edge of each window; NaN for the warm-up. Mirrors
    ``~/Desktop/datacryp/_scripts/_build_derived_v4.py::_rolling_split_diff``."""
    n = len(values)
    out = np.full(n, np.nan)
    if n < window:
        return out
    vw = sliding_window_view(values, window)
    sw = sliding_window_view(sortkey, window)
    k = max(1, int(k_frac * window))
    order = np.argsort(sw, axis=1)
    low = np.take_along_axis(vw, order[:, :k], axis=1)
    high = np.take_along_axis(vw, order[:, -k:], axis=1)
    out[window - 1:] = np.nanmean(high, axis=1) - np.nanmean(low, axis=1)
    return out


def ideal_amp(
    high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame, window: int = 20, k_frac: float = 0.25,
) -> pd.DataFrame:
    """理想振幅 (ideal amplitude): within a trailing window of ``window`` VALID
    bars (NaN bars are skipped entirely, not counted toward the window),
    split by CLOSE level into the top-k / bottom-k days (k = int(k_frac *
    window) by count), then mean(amplitude) on the high-close days minus
    mean(amplitude) on the low-close days. amplitude = high/low - 1, clipped
    at 300%. A symbol needs at least window+5 valid bars before it produces
    any output. Matches
    ``~/Desktop/datacryp/_scripts/_build_derived_v4.py::build_amplitude()``
    exactly (formula confirmed against ``docs/DATA_DICTIONARY.md``)."""
    amp = (high / low - 1.0).clip(upper=3.0)
    result = pd.DataFrame(np.nan, index=close.index, columns=close.columns, dtype="float64")
    for symbol in close.columns:
        a = amp[symbol].to_numpy()
        c = close[symbol].to_numpy()
        mask = ~(np.isnan(a) | np.isnan(c))
        if mask.sum() < window + 5:
            continue
        out = _rolling_split_diff(a[mask], c[mask], window, k_frac)
        result.iloc[np.where(mask)[0], result.columns.get_loc(symbol)] = out
    return result

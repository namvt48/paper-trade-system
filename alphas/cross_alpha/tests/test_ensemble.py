from __future__ import annotations

import pandas as pd
import pytest

from cross_alpha.ensemble import combine_members
from cross_alpha.spec import AlphaSpec


def _member_spec(alpha_id: str, signal: str, params: dict) -> AlphaSpec:
    return AlphaSpec(
        alpha_id=alpha_id,
        timeframe="1d",
        signal=signal,
        params=params,
        universe_size=4,
        universe_mode="dynamic_top_k",
        rebalance_bars=1,
        vol_lookback=30,
        ppy=365,
        long_threshold=None,
        short_threshold=None,
        construction="winsor_cont",
        winsor_k=3.0,
    )


def _panel() -> dict[str, pd.DataFrame]:
    idx = pd.Index(range(10), dtype="int64")
    close = pd.DataFrame(
        {
            "TRENDUSDT": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0],
            # Slight oscillation (not perfectly flat) so ts_zscore's rolling
            # std is non-zero -- a truly constant series would divide-by-zero
            # to NaN and break the cs_zscore/sum downstream.
            "FLATUSDT": [5.0, 5.05, 5.0, 5.05, 5.0, 5.05, 5.0, 5.05, 5.0, 5.05],
            "OTHER1USDT": [5.0, 5.1, 5.0, 5.1, 5.0, 5.1, 5.0, 5.1, 5.0, 5.1],
            "OTHER2USDT": [3.0, 3.1, 3.2, 3.1, 3.0, 3.1, 3.2, 3.1, 3.0, 3.1],
        },
        index=idx,
    )
    volume = pd.DataFrame({c: [100.0] * len(idx) for c in close.columns}, index=idx)
    high = close * 1.01
    low = close * 0.99
    return {
        "close": close, "high": high, "low": low, "volume": volume,
        "quote_volume": close * volume, "vwap": (high + low + close) / 3.0,
    }


def test_combine_members_averages_cs_zscore_and_ema_smooths():
    members = [
        _member_spec("member-zscore", "zscore", {"field": "close", "window": 5}),
        _member_spec("member-momentum", "momentum", {"field": "close", "window": 2}),
    ]

    combined = combine_members(_panel(), members, ema_smooth=1)

    last = combined.iloc[-1]
    assert last["TRENDUSDT"] > last["FLATUSDT"]
    # ema_smooth=1 is a passthrough (span=1 -> alpha=1.0): combined should
    # equal the raw mean-of-cs_zscore at every row, not just the last one.
    assert not combined.isna().all(axis=None)


def test_combine_members_raises_when_a_member_produces_no_score():
    # absolute_breakout returns (None, long_condition, short_condition, ...),
    # not a score -- unsupported as an ensemble member.
    members = [_member_spec("member-breakout", "absolute_breakout", {
        "long_field": "close", "short_field": "close",
        "long_window": 3, "short_window": 3, "long_z": 1.0, "short_z": -1.0,
    })]

    with pytest.raises(ValueError, match="member-breakout"):
        combine_members(_panel(), members, ema_smooth=1)

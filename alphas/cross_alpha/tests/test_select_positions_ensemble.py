from __future__ import annotations

import pandas as pd
import pytest

from cross_alpha.spec import AlphaSpec
from cross_alpha.strategy import select_positions


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


def _ensemble_spec(overlay: dict | None) -> AlphaSpec:
    return AlphaSpec(
        alpha_id="test-ensemble",
        timeframe="1d",
        signal="ensemble_mean",
        params={},
        universe_size=4,
        universe_mode="dynamic_top_k",
        rebalance_bars=1,
        vol_lookback=30,
        ppy=365,
        long_threshold=None,
        short_threshold=None,
        construction="winsor_cont",
        winsor_k=3.0,
        members=["member-zscore", "member-momentum"],
        overlay=overlay,
        ema_smooth=1,
    )


def _panel() -> dict[str, pd.DataFrame]:
    idx = pd.Index(range(40), dtype="int64")
    close = pd.DataFrame(
        {
            "TRENDUSDT": [1.0 + 0.1 * i for i in range(40)],
            "FLATUSDT": [5.0, 5.05] * 20,
            "OTHER1USDT": [5.0, 5.1] * 20,
            "OTHER2USDT": [3.0, 3.1, 3.2, 3.1] * 10,
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


def _members() -> list[AlphaSpec]:
    return [
        _member_spec("member-zscore", "zscore", {"field": "close", "window": 10}),
        _member_spec("member-momentum", "momentum", {"field": "close", "window": 5}),
    ]


def test_ensemble_without_overlay_uses_normal_winsor_cont_construction():
    spec = _ensemble_spec(overlay=None)

    selection = select_positions(_panel(), spec, member_specs=_members())

    assert selection.weights
    assert selection.longs
    assert selection.shorts
    assert selection.diagnostics["gross"] == pytest.approx(1.0)


def test_ensemble_requires_member_specs():
    spec = _ensemble_spec(overlay=None)

    with pytest.raises(ValueError, match="member_specs"):
        select_positions(_panel(), spec, member_specs=None)


def test_ensemble_with_overlay_applies_per_coin_cap():
    overlay = {
        "risk_parity": {"vol_lookback": 20},
        "per_coin_cap": 0.3,
    }
    spec = _ensemble_spec(overlay=overlay)

    selection = select_positions(_panel(), spec, member_specs=_members())

    assert selection.weights
    assert max(abs(w) for w in selection.weights.values()) <= 0.3 + 1e-9


def test_ensemble_with_overlay_drawdown_throttle_scales_down_weights():
    overlay = {
        "risk_parity": {"vol_lookback": 20},
        "drawdown_throttle": {"floor": -0.08, "factor": 0.4},
    }
    spec = _ensemble_spec(overlay=overlay)
    members = _members()
    panel = _panel()

    normal = select_positions(panel, spec, member_specs=members, current_drawdown=-0.02)
    throttled = select_positions(panel, spec, member_specs=members, current_drawdown=-0.10)

    common = set(normal.weights) & set(throttled.weights)
    assert common
    for symbol in common:
        assert throttled.weights[symbol] == pytest.approx(normal.weights[symbol] * 0.4)

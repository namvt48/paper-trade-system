from __future__ import annotations

import pytest

from cross_alpha.spec import AlphaSpec


def _base_kwargs(**overrides):
    kwargs = dict(
        alpha_id="test-ensemble",
        timeframe="1d",
        signal="ensemble_mean",
        params={},
        universe_size=180,
        universe_mode="dynamic_top_k",
        rebalance_bars=1,
        vol_lookback=30,
        ppy=365,
        long_threshold=None,
        short_threshold=None,
    )
    kwargs.update(overrides)
    return kwargs


def test_ensemble_fields_default_to_none():
    spec = AlphaSpec(**_base_kwargs())
    assert spec.members is None
    assert spec.overlay is None
    assert spec.ema_smooth is None


def test_ensemble_fields_accept_explicit_values():
    overlay = {
        "risk_parity": {"vol_lookback": 30},
        "beta_neutralize": {"window": 60},
        "per_coin_cap": 0.04,
        "drawdown_throttle": {"floor": -0.08, "factor": 0.4},
    }
    spec = AlphaSpec(**_base_kwargs(
        members=["1d-trend60cmf", "1d-kertrend", "1d-vwaprev", "1d-chmom"],
        overlay=overlay,
        ema_smooth=5,
    ))
    assert spec.members == ["1d-trend60cmf", "1d-kertrend", "1d-vwaprev", "1d-chmom"]
    assert spec.overlay == overlay
    assert spec.ema_smooth == 5


def test_ensemble_required_bars_raises_informative_error():
    # required_bars is computed from this spec's own params alone; an
    # ensemble's true warmup need depends on its members' own required_bars,
    # which only the caller (holding the alphas root path) can resolve.
    spec = AlphaSpec(**_base_kwargs(members=["1d-trend60cmf"], ema_smooth=5))
    with pytest.raises(ValueError, match="members"):
        _ = spec.required_bars

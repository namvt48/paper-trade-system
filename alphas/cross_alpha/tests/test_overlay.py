from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cross_alpha.overlay import beta_neutralize, drawdown_throttle, per_coin_cap, risk_parity


def test_risk_parity_tilts_toward_lower_vol_symbol_and_normalizes_gross():
    idx = pd.Index(range(40), dtype="int64")
    rng = np.random.default_rng(42)
    returns = pd.DataFrame(
        {
            "LOWVOLUSDT": rng.normal(0, 0.001, size=len(idx)),
            "HIGHVOLUSDT": rng.normal(0, 0.05, size=len(idx)),
        },
        index=idx,
    )
    signal = pd.Series({"LOWVOLUSDT": 1.0, "HIGHVOLUSDT": 1.0})

    weights = risk_parity(signal, returns, vol_lookback=30)

    assert weights["LOWVOLUSDT"] > weights["HIGHVOLUSDT"] > 0
    assert weights.abs().sum() == pytest.approx(1.0)


def test_risk_parity_drops_symbols_with_zero_realized_vol():
    idx = pd.Index(range(40), dtype="int64")
    returns = pd.DataFrame(
        {
            "FLATUSDT": [0.0] * len(idx),
            "MOVERUSDT": [0.01 if i % 2 == 0 else -0.01 for i in range(len(idx))],
        },
        index=idx,
    )
    signal = pd.Series({"FLATUSDT": 1.0, "MOVERUSDT": 1.0})

    weights = risk_parity(signal, returns, vol_lookback=30)

    assert "FLATUSDT" not in weights
    assert weights["MOVERUSDT"] == pytest.approx(1.0)


def test_beta_neutralize_zeroes_out_portfolio_beta():
    idx = pd.Index(range(30), dtype="int64")
    market = pd.Series(np.linspace(-0.02, 0.02, len(idx)), index=idx)
    returns = pd.DataFrame(
        {
            "HIBETAUSDT": market * 2.0,
            "LOBETAUSDT": market * 0.5,
        },
        index=idx,
    )
    weights = pd.Series({"HIBETAUSDT": 0.5, "LOBETAUSDT": 0.5})

    adjusted = beta_neutralize(weights, returns, window=30)

    tail = returns.tail(30)
    mkt = tail.mean(axis=1)
    beta = tail.apply(lambda col: col.cov(mkt) / mkt.var())
    portfolio_beta = float((adjusted * beta).sum())
    assert abs(portfolio_beta) < 1e-6


def test_per_coin_cap_clips_outliers_to_the_cap_without_rescaling():
    # per_coin_cap is a hard risk control, not a gross-preserving rescale --
    # gross exposure is expected to drop when the cap actually binds.
    weights = pd.Series({"AUSDT": 0.20, "BUSDT": -0.05, "CUSDT": 0.02})

    capped = per_coin_cap(weights, cap=0.04)

    assert capped["AUSDT"] == pytest.approx(0.04)
    assert capped["BUSDT"] == pytest.approx(-0.04)
    assert capped["CUSDT"] == pytest.approx(0.02)
    assert capped.abs().max() <= 0.04 + 1e-9


def test_per_coin_cap_noop_when_nothing_exceeds_cap():
    weights = pd.Series({"AUSDT": 0.02, "BUSDT": -0.03})

    capped = per_coin_cap(weights, cap=0.04)

    assert capped.equals(weights)


def test_drawdown_throttle_cuts_exposure_below_floor():
    weights = pd.Series({"AUSDT": 0.5, "BUSDT": -0.5})

    throttled = drawdown_throttle(weights, current_drawdown=-0.10, floor=-0.08, factor=0.4)

    assert throttled["AUSDT"] == pytest.approx(0.2)
    assert throttled["BUSDT"] == pytest.approx(-0.2)


def test_drawdown_throttle_passes_through_above_floor():
    weights = pd.Series({"AUSDT": 0.5, "BUSDT": -0.5})

    throttled = drawdown_throttle(weights, current_drawdown=-0.03, floor=-0.08, factor=0.4)

    assert throttled.equals(weights)

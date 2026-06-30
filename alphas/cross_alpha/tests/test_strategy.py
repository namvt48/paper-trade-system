from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from cross_alpha.spec import AlphaSpec
from cross_alpha.strategy import select_positions


def _panel_for_winsor_cont() -> dict[str, pd.DataFrame]:
    symbols = ["S0USDT", "S1USDT", "S2USDT", "S3USDT", "S4USDT", "S5USDT"]
    idx = pd.Index(range(5), dtype="int64")
    close = pd.DataFrame(
        {
            "S0USDT": [1.0, 1.0, 1.0, 1.0, 2.0],
            "S1USDT": [1.0, 2.0, 3.0, 4.0, 5.0],
            "S2USDT": [5.0, 4.0, 3.0, 2.0, 1.0],
            "S3USDT": [2.0, 2.0, 2.0, 2.0, 1.0],
            "S4USDT": [3.0, 3.0, 3.0, 3.0, 4.0],
            "S5USDT": [4.0, 4.0, 4.0, 4.0, 3.0],
        },
        index=idx,
    )
    volume = pd.DataFrame(
        {
            "S0USDT": [6000.0] * len(idx),
            "S1USDT": [5000.0] * len(idx),
            "S2USDT": [4000.0] * len(idx),
            "S3USDT": [3000.0] * len(idx),
            "S4USDT": [20.0] * len(idx),
            "S5USDT": [10.0] * len(idx),
        },
        index=idx,
    )
    high = close * 1.01
    low = close * 0.99
    return {
        "close": close,
        "high": high,
        "low": low,
        "volume": volume,
        "quote_volume": close * volume,
        "vwap": (high + low + close) / 3.0,
    }


def test_winsor_cont_preserves_scaled_weights():
    spec = AlphaSpec(
        alpha_id="test-winsor",
        timeframe="1h",
        signal="zscore",
        params={"field": "close", "window": 5},
        universe_size=4,
        universe_mode="dynamic_top_k",
        rebalance_bars=24,
        exec_lag=1,
        vol_lookback=20,
        ppy=8760,
        long_threshold=None,
        short_threshold=None,
        construction="winsor_cont",
        winsor_k=3.0,
    )

    selection = select_positions(_panel_for_winsor_cont(), spec)

    assert selection.weights
    assert len(selection.weights) == 4
    assert len(selection.longs) + len(selection.shorts) == 4
    assert selection.longs
    assert selection.shorts
    assert selection.diagnostics["gross"] == pytest.approx(1.0)
    assert abs(selection.diagnostics["net"]) < 1e-12
    assert all(symbol in {"S0USDT", "S1USDT", "S2USDT", "S3USDT"} for symbol in selection.weights)
    assert all(selection.indicators[symbol]["target_weight"] != 0.0 for symbol in selection.weights)


@pytest.mark.parametrize(
    ("alpha_id", "timeframe", "rebalance_bars"),
    [
        ("15m-blend-close-2-v3", "15m", 192),
        ("1h-decay-close-v3", "1h", 48),
        ("4h-trend-close-v3", "4h", 12),
    ],
)
def test_v3_specs_match_new_alpha_docs(alpha_id: str, timeframe: str, rebalance_bars: int):
    spec = AlphaSpec.load(Path(__file__).resolve().parents[2] / alpha_id / "spec.json")

    assert spec.timeframe == timeframe
    assert spec.universe_size == 180
    assert spec.universe_mode == "dynamic_top_k"
    assert spec.rebalance_bars == rebalance_bars
    assert spec.exec_lag == 0
    assert spec.long_threshold is None
    assert spec.short_threshold is None
    assert spec.construction == "winsor_cont"
    assert spec.winsor_k == 3.0


@pytest.mark.parametrize(
    ("alpha_id", "timeframe", "signal", "rebalance_bars", "warmup_bars"),
    [
        ("4h-trend-z", "4h", "zscore", 12, 180),
        ("15m-breakout", "15m", "breakout", 192, 2880),
        ("1h-trend-breakout", "1h", "blend_zscore_range", 48, 2880),
        ("4h-amihud", "4h", "amihud", 12, 181),
        ("1h-trend-skew", "1h", "blend_zscore_skew", 48, 2880),
        ("songthanv8", "1h", "zscore", 48, 720),
        ("songthanv11", "15m", "breakout_hl", 192, 2880),
    ],
)
def test_docs_alphas_specs(
    alpha_id: str, timeframe: str, signal: str, rebalance_bars: int, warmup_bars: int,
):
    spec = AlphaSpec.load(Path(__file__).resolve().parents[2] / alpha_id / "spec.json")

    assert spec.timeframe == timeframe
    assert spec.signal == signal
    assert spec.universe_size == 180
    assert spec.universe_mode == "dynamic_top_k"
    assert spec.rebalance_bars == rebalance_bars
    assert spec.exec_lag == 1
    assert spec.long_threshold is None
    assert spec.short_threshold is None
    assert spec.construction == "winsor_cont"
    assert spec.winsor_k == 3.0
    assert spec.target_vol == 0.1
    assert spec.max_leverage == 3.0
    assert spec.required_bars == warmup_bars


def test_breakout_signal_long_high_short_low():
    spec = AlphaSpec(
        alpha_id="test-breakout",
        timeframe="15m",
        signal="breakout",
        params={"window": 5},
        universe_size=4,
        universe_mode="dynamic_top_k",
        rebalance_bars=192,
        exec_lag=1,
        vol_lookback=20,
        ppy=35040,
        long_threshold=None,
        short_threshold=None,
        construction="winsor_cont",
        winsor_k=3.0,
    )

    selection = select_positions(_panel_for_winsor_cont(), spec)

    assert selection.weights
    assert selection.longs
    assert selection.shorts
    assert selection.diagnostics["gross"] == pytest.approx(1.0)
    assert abs(selection.diagnostics["net"]) < 1e-12
    assert "S1USDT" in selection.longs
    assert "S2USDT" in selection.shorts


def test_breakout_hl_signal_long_high_short_low():
    spec = AlphaSpec(
        alpha_id="test-breakout-hl",
        timeframe="15m",
        signal="breakout_hl",
        params={"window": 5},
        universe_size=4,
        universe_mode="dynamic_top_k",
        rebalance_bars=192,
        exec_lag=1,
        vol_lookback=20,
        ppy=35040,
        long_threshold=None,
        short_threshold=None,
        construction="winsor_cont",
        winsor_k=3.0,
    )

    selection = select_positions(_panel_for_winsor_cont(), spec)

    assert selection.weights
    assert selection.longs
    assert selection.shorts
    assert selection.diagnostics["gross"] == pytest.approx(1.0)
    assert abs(selection.diagnostics["net"]) < 1e-12
    assert "S1USDT" in selection.longs
    assert "S2USDT" in selection.shorts


def test_blend_zscore_range_signal_produces_longs_and_shorts():
    spec = AlphaSpec(
        alpha_id="test-trend-breakout",
        timeframe="1h",
        signal="blend_zscore_range",
        params={"z_window": 5, "range_window": 5},
        universe_size=4,
        universe_mode="dynamic_top_k",
        rebalance_bars=48,
        exec_lag=1,
        vol_lookback=20,
        ppy=8760,
        long_threshold=None,
        short_threshold=None,
        construction="winsor_cont",
        winsor_k=3.0,
    )

    selection = select_positions(_panel_for_winsor_cont(), spec)

    assert selection.weights
    assert selection.longs
    assert selection.shorts
    assert selection.diagnostics["gross"] == pytest.approx(1.0)
    assert abs(selection.diagnostics["net"]) < 1e-12


def test_amihud_signal_long_illiquid_short_liquid():
    spec = AlphaSpec(
        alpha_id="test-amihud",
        timeframe="4h",
        signal="amihud",
        params={"window": 4},
        universe_size=4,
        universe_mode="dynamic_top_k",
        rebalance_bars=12,
        exec_lag=1,
        vol_lookback=20,
        ppy=2190,
        long_threshold=None,
        short_threshold=None,
        construction="winsor_cont",
        winsor_k=3.0,
    )

    selection = select_positions(_panel_for_winsor_cont(), spec)

    assert selection.weights
    assert selection.longs
    assert selection.shorts
    assert len(selection.longs) == len(selection.shorts)
    assert selection.diagnostics["gross"] == pytest.approx(1.0)
    assert "S2USDT" in selection.longs
    assert "S0USDT" in selection.shorts

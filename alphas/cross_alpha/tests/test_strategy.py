from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from cross_alpha.spec import AlphaSpec
from cross_alpha.strategy import (
    build_funding_panel,
    resample_funding_to_native_cadence,
    select_positions,
)


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
    assert all(
        symbol in {"S0USDT", "S1USDT", "S2USDT", "S3USDT"}
        for symbol in selection.weights
    )
    assert all(
        selection.indicators[symbol]["target_weight"] != 0.0
        for symbol in selection.weights
    )


@pytest.mark.parametrize(
    ("alpha_id", "timeframe", "rebalance_bars"),
    [
        ("15m-blend-close-2-v3", "15m", 192),
        ("1h-decay-close-v3", "1h", 48),
        ("4h-trend-close-v3", "4h", 12),
    ],
)
def test_v3_specs_match_new_alpha_docs(
    alpha_id: str, timeframe: str, rebalance_bars: int
):
    spec = AlphaSpec.load(Path(__file__).resolve().parents[2] / alpha_id / "spec.json")

    assert spec.timeframe == timeframe
    assert spec.universe_size == 180
    assert spec.universe_mode == "dynamic_top_k"
    assert spec.rebalance_bars == rebalance_bars
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
        ("1d-kertrend", "1d", "kaufman_trend", 1, 39),
        ("1d-trend60cmf", "1d", "trend_cmf_blend", 1, 60),
        ("1d-vwaprev", "1d", "vwap_reversion", 1, 20),
        ("1d-chmom", "1d", "carry_momentum", 1, 21),
        ("1d-iamp", "1d", "ideal_amplitude", 1, 25),
    ],
)
def test_docs_alphas_specs(
    alpha_id: str,
    timeframe: str,
    signal: str,
    rebalance_bars: int,
    warmup_bars: int,
):
    spec = AlphaSpec.load(Path(__file__).resolve().parents[2] / alpha_id / "spec.json")

    assert spec.timeframe == timeframe
    assert spec.signal == signal
    assert spec.universe_size == 180
    assert spec.universe_mode == "dynamic_top_k"
    assert spec.rebalance_bars == rebalance_bars
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


def test_kaufman_trend_signal_longs_clean_trend_shorts_choppy():
    idx = pd.Index(range(10), dtype="int64")
    close = pd.DataFrame(
        {
            "TRENDUSDT": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0],
            "CHOPUSDT": [5.0, 7.0, 5.0, 7.0, 5.0, 7.0, 5.0, 7.0, 5.0, 7.0],
            "DOWNUSDT": [10.0, 9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.0],
            "CHOP2USDT": [4.0, 6.0, 4.0, 6.0, 4.0, 6.0, 4.0, 6.0, 4.0, 6.0],
        },
        index=idx,
    )
    volume = pd.DataFrame({c: [100.0] * len(idx) for c in close.columns}, index=idx)
    high = close * 1.01
    low = close * 0.99
    panel = {
        "close": close,
        "high": high,
        "low": low,
        "volume": volume,
        "quote_volume": close * volume,
        "vwap": (high + low + close) / 3.0,
    }
    spec = AlphaSpec(
        alpha_id="test-kertrend",
        timeframe="1d",
        signal="kaufman_trend",
        params={"field": "close", "er_window": 4, "ema_span": 3},
        universe_size=4,
        universe_mode="dynamic_top_k",
        rebalance_bars=1,
        vol_lookback=20,
        ppy=365,
        long_threshold=None,
        short_threshold=None,
        construction="winsor_cont",
        winsor_k=3.0,
    )

    selection = select_positions(panel, spec)

    assert selection.weights
    assert selection.scores["TRENDUSDT"] > selection.scores["CHOPUSDT"]
    assert selection.scores["DOWNUSDT"] > selection.scores["CHOPUSDT"]
    assert selection.diagnostics["gross"] == pytest.approx(1.0)


def _panel_for_trend_cmf_blend() -> dict[str, pd.DataFrame]:
    # CMF needs close to sit asymmetrically within the bar's high-low range, so
    # unlike _panel_for_winsor_cont() (high/low symmetric around close, which
    # makes the money-flow multiplier -- and therefore CMF -- identically zero
    # for every symbol), each symbol here has a distinct close-within-range
    # placement as well as a distinct price trend.
    idx = pd.Index(range(10), dtype="int64")
    close = pd.DataFrame(
        {
            "AUSDT": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0],
            "BUSDT": [10.0, 9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.0],
            "CUSDT": [5.0] * 10,
            "DUSDT": [5.0, 5.5, 5.0, 5.5, 5.0, 5.5, 5.0, 5.5, 5.0, 5.5],
        },
        index=idx,
    )
    high = pd.DataFrame(
        {
            "AUSDT": close["AUSDT"] + 0.1,
            "BUSDT": close["BUSDT"] + 1.0,
            "CUSDT": close["CUSDT"] + 0.5,
            "DUSDT": close["DUSDT"] + 0.5,
        },
        index=idx,
    )
    low = pd.DataFrame(
        {
            "AUSDT": close["AUSDT"] - 1.0,
            "BUSDT": close["BUSDT"] - 0.1,
            "CUSDT": close["CUSDT"] - 0.5,
            "DUSDT": close["DUSDT"] - 0.5,
        },
        index=idx,
    )
    volume = pd.DataFrame({c: [100.0] * len(idx) for c in close.columns}, index=idx)
    return {
        "close": close,
        "high": high,
        "low": low,
        "volume": volume,
        "quote_volume": close * volume,
        "vwap": (high + low + close) / 3.0,
    }


def test_trend_cmf_blend_signal_produces_longs_and_shorts():
    spec = AlphaSpec(
        alpha_id="test-trend60cmf",
        timeframe="1d",
        signal="trend_cmf_blend",
        params={"z_window": 5, "cmf_window": 3, "ema_span": 3},
        universe_size=4,
        universe_mode="dynamic_top_k",
        rebalance_bars=1,
        vol_lookback=20,
        ppy=365,
        long_threshold=None,
        short_threshold=None,
        construction="winsor_cont",
        winsor_k=3.0,
    )

    selection = select_positions(_panel_for_trend_cmf_blend(), spec)

    # Note: unlike the balanced-panel blend tests above, this fixture's
    # long/short counts are naturally unequal, so select_positions()'s
    # winsor_cont long==short trim (strategy.py) kicks in and the trimmed
    # weights are not guaranteed to net to zero -- only gross==1 is invariant
    # post-trim (see the re-normalisation in strategy.py). Structural checks
    # only; exact symbol membership isn't asserted since this is a 2-term
    # blend, not a simple monotone signal.
    assert selection.weights
    assert selection.longs
    assert selection.shorts
    assert selection.diagnostics["gross"] == pytest.approx(1.0)


def test_vwap_reversion_signal_longs_breakout_shorts_breakdown():
    idx = pd.Index(range(10), dtype="int64")
    close = pd.DataFrame(
        {
            "BREAKOUTUSDT": [10.0] * 8
            + [20.0, 25.0],  # stretched far above its own VWAP
            "BREAKDOWNUSDT": [10.0] * 8
            + [5.0, 3.0],  # stretched far below its own VWAP
            "FLATUSDT": [10.0] * 10,
            "FLAT2USDT": [12.0] * 10,
        },
        index=idx,
    )
    volume = pd.DataFrame({c: [100.0] * len(idx) for c in close.columns}, index=idx)
    high = close * 1.01
    low = close * 0.99
    panel = {
        "close": close,
        "high": high,
        "low": low,
        "volume": volume,
        "quote_volume": close * volume,
        "vwap": (high + low + close) / 3.0,
    }
    spec = AlphaSpec(
        alpha_id="test-vwaprev",
        timeframe="1d",
        signal="vwap_reversion",
        params={"vwap_window": 5, "ema_span": 1},
        universe_size=4,
        universe_mode="dynamic_top_k",
        rebalance_bars=1,
        vol_lookback=20,
        ppy=365,
        long_threshold=None,
        short_threshold=None,
        construction="winsor_cont",
        winsor_k=3.0,
    )

    selection = select_positions(panel, spec)

    assert selection.weights
    assert selection.scores["BREAKOUTUSDT"] > selection.scores["FLATUSDT"]
    assert selection.scores["BREAKDOWNUSDT"] < selection.scores["FLATUSDT"]
    assert selection.diagnostics["gross"] == pytest.approx(1.0)


def test_build_funding_panel_sorts_by_time_and_skips_missing_symbols():
    snapshot = {
        "BTCUSDT": [
            {"funding_time": 2_000, "funding_rate": 0.0002},
            {"funding_time": 1_000, "funding_rate": 0.0001},
        ],
        "ETHUSDT": None,
        "SOLUSDT": [],
    }

    panel = build_funding_panel(snapshot)

    assert list(panel.index) == [1_000, 2_000]
    assert list(panel.columns) == ["BTCUSDT"]
    assert panel.loc[1_000, "BTCUSDT"] == pytest.approx(0.0001)
    assert panel.loc[2_000, "BTCUSDT"] == pytest.approx(0.0002)


_EIGHT_HOURS_MS = 8 * 3600 * 1000


def test_resample_funding_to_native_cadence_is_noop_for_already_8h_symbols():
    idx = pd.Index([0, _EIGHT_HOURS_MS, 2 * _EIGHT_HOURS_MS], dtype="int64")
    funding = pd.DataFrame({"BTCUSDT": [0.0001, 0.0002, 0.0003]}, index=idx)

    resampled = resample_funding_to_native_cadence(funding)

    pd.testing.assert_frame_equal(resampled, funding)


def test_resample_funding_to_native_cadence_keeps_last_reading_per_bucket():
    # ALTUSDT settles every 2h -- 4 readings land inside bucket 0 (the first
    # 8h window); only the latest of those four should survive.
    idx = pd.Index(
        [0, 2 * 3600 * 1000, 4 * 3600 * 1000, 6 * 3600 * 1000, _EIGHT_HOURS_MS],
        dtype="int64",
    )
    funding = pd.DataFrame(
        {"ALTUSDT": [0.0001, 0.0002, 0.0003, 0.0004, 0.0005]}, index=idx
    )

    resampled = resample_funding_to_native_cadence(funding)

    assert list(resampled.index) == [0, _EIGHT_HOURS_MS]
    assert resampled.loc[0, "ALTUSDT"] == pytest.approx(0.0004)
    assert resampled.loc[_EIGHT_HOURS_MS, "ALTUSDT"] == pytest.approx(0.0005)


def test_resample_funding_to_native_cadence_empty_panel_is_noop():
    empty = pd.DataFrame()

    assert resample_funding_to_native_cadence(empty).empty


def test_carry_momentum_signal_longs_uptrend_cheap_funding_shorts_expensive():
    idx = pd.Index(range(25), dtype="int64")
    close = pd.DataFrame(
        {
            "CHEAPUSDT": [
                float(i) for i in range(1, 26)
            ],  # strong uptrend, cheap funding
            "RICHUSDT": [
                float(i) for i in range(1, 26)
            ],  # strong uptrend, expensive funding
            "FLATUSDT": [5.0] * 25,
            "FLAT2USDT": [6.0] * 25,
        },
        index=idx,
    )
    volume = pd.DataFrame({c: [100.0] * len(idx) for c in close.columns}, index=idx)
    high = close * 1.01
    low = close * 0.99
    # carry_momentum now consumes an already-computed funding_zscore panel
    # directly (the runner's _attach_funding_panel does the ts_zscore step,
    # at funding's own native settlement frequency, before this panel is
    # ever built) -- so this fixture supplies zscore-like values straight,
    # no baseline oscillation needed since no zscore is computed in here.
    funding_zscore = pd.DataFrame(
        {
            "CHEAPUSDT": [0.0] * 25,
            "RICHUSDT": [2.5] * 25,  # "just got crowded" -- high funding_zscore
            "FLATUSDT": [0.0] * 25,
            "FLAT2USDT": [0.0] * 25,
        },
        index=idx,
    )
    panel = {
        "close": close,
        "high": high,
        "low": low,
        "volume": volume,
        "quote_volume": close * volume,
        "vwap": (high + low + close) / 3.0,
        "funding_zscore": funding_zscore,
    }
    spec = AlphaSpec(
        alpha_id="test-chmom",
        timeframe="1d",
        signal="carry_momentum",
        params={"momentum_window": 5, "funding_window": 10, "ema_span": 3},
        universe_size=4,
        universe_mode="dynamic_top_k",
        rebalance_bars=1,
        vol_lookback=20,
        ppy=365,
        long_threshold=None,
        short_threshold=None,
        construction="winsor_cont",
        winsor_k=3.0,
        needs_funding=True,
    )

    selection = select_positions(panel, spec)

    assert selection.weights
    assert selection.scores["CHEAPUSDT"] > selection.scores["RICHUSDT"]
    assert selection.diagnostics["gross"] == pytest.approx(1.0)


def test_ideal_amplitude_signal_longs_positive_amp_close_correlation():
    idx = pd.Index(range(9), dtype="int64")
    close = pd.DataFrame(
        {
            "POSAMPUSDT": [float(i) for i in range(1, 10)],  # 1..9
            "NEGAMPUSDT": [float(i) for i in range(1, 10)],
        },
        index=idx,
    )
    # POSAMPUSDT: amplitude rises together with close (high-close days have
    # the biggest daily range) -> positive ideal_amp.
    # NEGAMPUSDT: amplitude falls as close rises (high-close days have the
    # SMALLEST range) -> negative ideal_amp.
    low = pd.DataFrame(
        {"POSAMPUSDT": [100.0] * 9, "NEGAMPUSDT": [100.0] * 9}, index=idx
    )
    pos_amp = close["POSAMPUSDT"] / 100.0
    neg_amp = (10.0 - close["NEGAMPUSDT"]) / 100.0
    high = pd.DataFrame(
        {
            "POSAMPUSDT": (low["POSAMPUSDT"] * (1 + pos_amp)).to_numpy(),
            "NEGAMPUSDT": (low["NEGAMPUSDT"] * (1 + neg_amp)).to_numpy(),
        },
        index=idx,
    )
    volume = pd.DataFrame({c: [100.0] * len(idx) for c in close.columns}, index=idx)
    panel = {
        "close": close,
        "high": high,
        "low": low,
        "volume": volume,
        "quote_volume": close * volume,
        "vwap": (high + low + close) / 3.0,
    }
    spec = AlphaSpec(
        alpha_id="test-iamp",
        timeframe="1d",
        signal="ideal_amplitude",
        params={"window": 4, "k_frac": 0.25, "ema_span": 1},
        universe_size=2,
        universe_mode="dynamic_top_k",
        rebalance_bars=1,
        vol_lookback=20,
        ppy=365,
        long_threshold=None,
        short_threshold=None,
        construction="winsor_cont",
        winsor_k=3.0,
    )

    selection = select_positions(panel, spec)

    assert selection.weights
    assert selection.scores["POSAMPUSDT"] > selection.scores["NEGAMPUSDT"]
    assert selection.diagnostics["gross"] == pytest.approx(1.0)


def test_amihud_signal_long_illiquid_short_liquid():
    spec = AlphaSpec(
        alpha_id="test-amihud",
        timeframe="4h",
        signal="amihud",
        params={"window": 4},
        universe_size=4,
        universe_mode="dynamic_top_k",
        rebalance_bars=12,
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


def test_trend60cmf_has_its_own_137_symbol_whitelist():
    # 1d-trend60cmf uses a narrower, user-provided liquidity-filtered
    # whitelist (matching the doc's own "TRADABLE, ~137 coin" note) instead
    # of the shared 199-symbol one the other new alphas use. Locked here so
    # a future "sync all whitelists" pass doesn't silently overwrite it.
    alphas_root = Path(__file__).resolve().parents[2]
    trend60cmf_symbols = [
        line.strip()
        for line in (alphas_root / "1d-trend60cmf" / "whitelist.txt")
        .read_text()
        .splitlines()
        if line.strip()
    ]
    shared_symbols = {
        line.strip()
        for line in (alphas_root / "1h-decay-close" / "whitelist.txt")
        .read_text()
        .splitlines()
        if line.strip()
    }

    assert len(trend60cmf_symbols) == 137
    assert len(set(trend60cmf_symbols)) == 137  # no duplicates
    assert all(s.endswith("USDT") and s == s.upper() for s in trend60cmf_symbols)
    assert set(trend60cmf_symbols) <= shared_symbols  # subset -- catches typos


def _dyn_topk_spec() -> AlphaSpec:
    return AlphaSpec(
        alpha_id="test-ctx",
        timeframe="1d",
        signal="zscore",
        params={"field": "close", "window": 3},
        universe_size=4,
        universe_mode="dynamic_top_k",
        rebalance_bars=1,
        vol_lookback=20,
        ppy=365,
        long_threshold=None,
        short_threshold=None,
    )


def test_invalidate_masked_fields_sees_late_attached_field():
    # Repro of the shared-context bug: the daily 197-universe alphas share ONE
    # CrossAlphaComputeContext. A non-funding alpha memoizes the dynamic_top_k
    # masked snapshot first; a funding alpha then attaches funding_zscore to the
    # SAME panel dict. Without invalidation the stale snapshot never sees it ->
    # compute_signal_details raises KeyError('funding_zscore').
    from cross_alpha.strategy import CrossAlphaComputeContext

    panel = _panel_for_winsor_cont()
    ctx = CrossAlphaComputeContext(panel)
    spec = _dyn_topk_spec()

    # non-funding alpha computes first -> memoizes snapshot without funding_zscore
    fields_before, _ = ctx.masked_fields(spec)
    assert "funding_zscore" not in fields_before

    # funding alpha attaches funding_zscore to the shared panel AFTER the snapshot
    panel["funding_zscore"] = panel["close"] * 0.0

    # BUG: cached snapshot still lacks it
    stale, _ = ctx.masked_fields(spec)
    assert "funding_zscore" not in stale

    # FIX: invalidate -> rebuild -> field is now visible (and masked like the rest)
    ctx.invalidate_masked_fields()
    fixed, _ = ctx.masked_fields(spec)
    assert "funding_zscore" in fixed


# --- winsor_cont Top-K + power sizing (spec.top_k / spec.power_p) ----------


def _panel_for_topk_power() -> dict[str, pd.DataFrame]:
    # Momentum (window=1) on the LAST bar is exactly the chosen cross-section
    # [+0.6, -0.6, +0.2, -0.2, 0.0]. Column order pairs +v/-v so the
    # cross-section mean is exactly 0 and the z-scores work out by hand to
    # +/-sqrt(1.8) (BIG*), +/-sqrt(0.2) (SMALL*), 0 (FLAT).
    idx = pd.Index(range(3), dtype="int64")
    close = pd.DataFrame(
        {
            "BIGUPUSDT": [1.0, 1.0, 1.6],
            "BIGDOWNUSDT": [1.0, 1.0, 0.4],
            "SMALLUPUSDT": [1.0, 1.0, 1.2],
            "SMALLDOWNUSDT": [1.0, 1.0, 0.8],
            "FLATUSDT": [1.0, 1.0, 1.0],
        },
        index=idx,
    )
    volume = pd.DataFrame({c: [100.0] * len(idx) for c in close.columns}, index=idx)
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


def _topk_spec(
    top_k: int | None = 2, power_p: float = 2.0, winsor_k: float = 3.0
) -> AlphaSpec:
    return AlphaSpec(
        alpha_id="test-winsor-topk",
        timeframe="1d",
        signal="momentum",
        params={"field": "close", "window": 1},
        universe_size=5,
        universe_mode="all",
        rebalance_bars=1,
        vol_lookback=20,
        ppy=365,
        long_threshold=None,
        short_threshold=None,
        construction="winsor_cont",
        winsor_k=winsor_k,
        top_k=top_k,
        power_p=power_p,
    )


def test_topk_power2_exact_hand_computed_weights():
    # z = +/-sqrt(1.8) (BIG*), +/-sqrt(0.2) (SMALL*), 0 (FLAT, excluded).
    # top_k=2 per side, power_p=2 -> raw = z^2 = [1.8, 0.2] per side,
    # sum(|raw|) = 4.0 -> weights 1.8/4 = 0.45 and 0.2/4 = 0.05.
    selection = select_positions(
        _panel_for_topk_power(), _topk_spec(top_k=2, power_p=2.0)
    )

    assert selection.longs == ["BIGUPUSDT", "SMALLUPUSDT"]
    assert selection.shorts == ["BIGDOWNUSDT", "SMALLDOWNUSDT"]
    assert len(selection.longs) == 2
    assert len(selection.shorts) == 2
    assert selection.weights["BIGUPUSDT"] == pytest.approx(0.45)
    assert selection.weights["SMALLUPUSDT"] == pytest.approx(0.05)
    assert selection.weights["BIGDOWNUSDT"] == pytest.approx(-0.45)
    assert selection.weights["SMALLDOWNUSDT"] == pytest.approx(-0.05)
    assert "FLATUSDT" not in selection.weights
    assert len(selection.weights) == 4
    assert selection.diagnostics["gross"] == pytest.approx(1.0)
    assert selection.diagnostics["net"] == pytest.approx(0.0, abs=1e-12)


def test_topk1_selects_exactly_one_per_side():
    selection = select_positions(
        _panel_for_topk_power(), _topk_spec(top_k=1, power_p=1.0)
    )

    assert selection.longs == ["BIGUPUSDT"]
    assert selection.shorts == ["BIGDOWNUSDT"]
    assert selection.weights["BIGUPUSDT"] == pytest.approx(0.5)
    assert selection.weights["BIGDOWNUSDT"] == pytest.approx(-0.5)
    assert selection.diagnostics["gross"] == pytest.approx(1.0)


def test_topk_power1_weights_proportional_to_abs_z():
    # power_p=1 -> weight ∝ |z|; |z| ratio BIG:SMALL = sqrt(1.8):sqrt(0.2)
    # = 3:1 -> per-side weights 3/8 and 1/8 (gross 1, balanced 2 vs 2).
    selection = select_positions(
        _panel_for_topk_power(), _topk_spec(top_k=2, power_p=1.0)
    )

    assert selection.weights["BIGUPUSDT"] == pytest.approx(0.375)
    assert selection.weights["SMALLUPUSDT"] == pytest.approx(0.125)
    assert selection.weights["BIGDOWNUSDT"] == pytest.approx(-0.375)
    assert selection.weights["SMALLDOWNUSDT"] == pytest.approx(-0.125)
    assert selection.diagnostics["gross"] == pytest.approx(1.0)


def test_topk_winsor_clip_caps_z_before_power():
    # winsor_k=1 clips |z|=sqrt(1.8) down to 1.0; |z|=sqrt(0.2) < 1 stays.
    # raw = [1.0, 0.2] per side, sum(|raw|) = 2.4 -> weights 1/2.4 = 5/12
    # and 0.2/2.4 = 1/12.
    selection = select_positions(
        _panel_for_topk_power(),
        _topk_spec(top_k=2, power_p=2.0, winsor_k=1.0),
    )

    assert selection.longs == ["BIGUPUSDT", "SMALLUPUSDT"]
    assert selection.shorts == ["BIGDOWNUSDT", "SMALLDOWNUSDT"]
    assert selection.weights["BIGUPUSDT"] == pytest.approx(5.0 / 12.0)
    assert selection.weights["SMALLUPUSDT"] == pytest.approx(1.0 / 12.0)
    assert selection.weights["BIGDOWNUSDT"] == pytest.approx(-5.0 / 12.0)
    assert selection.weights["SMALLDOWNUSDT"] == pytest.approx(-1.0 / 12.0)
    assert selection.diagnostics["gross"] == pytest.approx(1.0)


def test_topk_zero_std_cross_section_yields_empty_selection():
    # All-ident momentum (all 0) -> std == 0 -> every z NaN -> excluded.
    idx = pd.Index(range(3), dtype="int64")
    close = pd.DataFrame({f"FLAT{i}USDT": [2.0, 2.0, 2.0] for i in range(5)}, index=idx)
    volume = pd.DataFrame({c: [100.0] * len(idx) for c in close.columns}, index=idx)
    high = close * 1.01
    low = close * 0.99
    panel = {
        "close": close,
        "high": high,
        "low": low,
        "volume": volume,
        "quote_volume": close * volume,
        "vwap": (high + low + close) / 3.0,
    }

    selection = select_positions(panel, _topk_spec(top_k=2, power_p=2.0))

    assert selection.weights == {}
    assert selection.longs == []
    assert selection.shorts == []
    assert selection.diagnostics["gross"] == 0.0


def test_topk_unbalanced_sides_trim_weakest_and_renormalize():
    # Momentum [+0.3, +0.2, -0.6, 0.0]: mean -0.025, deviations from mean
    # [13/40, 9/40, -23/40, 1/40]. top_k=2 -> longs {P1, P2} (the tiny-F
    # positive never reaches top-2) vs shorts {N1}: unbalanced 2 vs 1 ->
    # trim the weakest long (P2), then gross-normalize the remaining pair.
    # |z| ratio P1:N1 = 13:23 (the shared std cancels) -> +13/36, -23/36.
    idx = pd.Index(range(3), dtype="int64")
    close = pd.DataFrame(
        {
            "P1USDT": [1.0, 1.0, 1.3],
            "P2USDT": [1.0, 1.0, 1.2],
            "N1USDT": [1.0, 1.0, 0.4],
            "FUSDT": [1.0, 1.0, 1.0],
        },
        index=idx,
    )
    volume = pd.DataFrame({c: [100.0] * len(idx) for c in close.columns}, index=idx)
    high = close * 1.01
    low = close * 0.99
    panel = {
        "close": close,
        "high": high,
        "low": low,
        "volume": volume,
        "quote_volume": close * volume,
        "vwap": (high + low + close) / 3.0,
    }

    selection = select_positions(panel, _topk_spec(top_k=2, power_p=1.0))

    assert selection.longs == ["P1USDT"]
    assert selection.shorts == ["N1USDT"]
    assert "P2USDT" not in selection.weights
    assert selection.weights["P1USDT"] == pytest.approx(13.0 / 36.0)
    assert selection.weights["N1USDT"] == pytest.approx(-23.0 / 36.0)
    assert selection.diagnostics["gross"] == pytest.approx(1.0)


def test_no_topk_keeps_legacy_full_cross_section_weights():
    # top_k=None (production default, e.g. 15m-blend-close): ALL non-zero-z
    # symbols are held with weight ∝ clipped z (NOT |z|^power_p) -- z ratio
    # BIG:SMALL = 3:1 -> +/-3/8 and +/-1/8; FLAT (z=0) is dropped.
    selection = select_positions(
        _panel_for_topk_power(), _topk_spec(top_k=None, power_p=1.0)
    )

    assert set(selection.weights) == {
        "BIGUPUSDT",
        "BIGDOWNUSDT",
        "SMALLUPUSDT",
        "SMALLDOWNUSDT",
    }
    assert selection.weights["BIGUPUSDT"] == pytest.approx(0.375)
    assert selection.weights["SMALLUPUSDT"] == pytest.approx(0.125)
    assert selection.weights["BIGDOWNUSDT"] == pytest.approx(-0.375)
    assert selection.weights["SMALLDOWNUSDT"] == pytest.approx(-0.125)
    assert "FLATUSDT" not in selection.weights
    assert selection.diagnostics["gross"] == pytest.approx(1.0)
    assert selection.diagnostics["net"] == pytest.approx(0.0, abs=1e-12)


def test_vwap_reversion_with_topk_longs_above_vwap_shorts_below():
    # Direction must match the ported docs: close > VWAP -> positive score ->
    # LONG (spec.reverse stays False; the runner wrapper applies reverse).
    idx = pd.Index(range(10), dtype="int64")
    close = pd.DataFrame(
        {
            "BREAKOUTUSDT": [10.0] * 8 + [20.0, 25.0],
            "BREAKDOWNUSDT": [10.0] * 8 + [5.0, 3.0],
            "FLATUSDT": [10.0] * 10,
            "FLAT2USDT": [12.0] * 10,
        },
        index=idx,
    )
    volume = pd.DataFrame({c: [100.0] * len(idx) for c in close.columns}, index=idx)
    high = close * 1.01
    low = close * 0.99
    panel = {
        "close": close,
        "high": high,
        "low": low,
        "volume": volume,
        "quote_volume": close * volume,
        "vwap": (high + low + close) / 3.0,
    }
    spec = AlphaSpec(
        alpha_id="test-vwaprev-topk",
        timeframe="1d",
        signal="vwap_reversion",
        params={"vwap_window": 5, "ema_span": 1},
        universe_size=4,
        universe_mode="dynamic_top_k",
        rebalance_bars=1,
        vol_lookback=20,
        ppy=365,
        long_threshold=None,
        short_threshold=None,
        construction="winsor_cont",
        winsor_k=3.0,
        top_k=1,
        power_p=2.0,
    )

    selection = select_positions(panel, spec)

    assert selection.longs == ["BREAKOUTUSDT"]
    assert selection.shorts == ["BREAKDOWNUSDT"]
    assert selection.weights["BREAKOUTUSDT"] > 0
    assert selection.weights["BREAKDOWNUSDT"] < 0
    assert selection.diagnostics["gross"] == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("alpha_id", "universe_size", "top_k", "power_p", "whitelist_size"),
    [
        ("1d-vwaprev-w50-top15-p20", 50, 15, 2.0, 50),
        ("1d-vwaprev-w80-top25-p15", 80, 25, 1.5, 80),
    ],
)
def test_1d_vwaprev_topk_spec_dirs_are_complete(
    alpha_id: str,
    universe_size: int,
    top_k: int,
    power_p: float,
    whitelist_size: int,
):
    alpha_dir = Path(__file__).resolve().parents[2] / alpha_id
    spec = AlphaSpec.load(alpha_dir / "spec.json")

    assert spec.alpha_id == alpha_id
    assert spec.timeframe == "1d"
    assert spec.signal == "vwap_reversion"
    assert spec.params == {"vwap_window": 20, "ema_span": 1}
    assert spec.universe_size == universe_size
    assert spec.universe_mode == "dynamic_top_k"
    assert spec.rebalance_bars == 1
    assert spec.vol_lookback == 30
    assert spec.ppy == 365
    assert spec.long_threshold is None
    assert spec.short_threshold is None
    assert spec.target_vol == 0.1
    assert spec.max_leverage == 3.0
    assert spec.fee_bps == 7.0
    assert spec.construction == "winsor_cont"
    assert spec.winsor_k == 3.0
    assert spec.top_k == top_k
    assert spec.power_p == power_p
    assert spec.reverse is False
    assert spec.required_bars == 20

    whitelist = [
        line.strip()
        for line in (alpha_dir / "whitelist.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert len(whitelist) == whitelist_size
    assert len(set(whitelist)) == whitelist_size
    assert all(
        symbol.endswith("USDT") and symbol == symbol.upper() for symbol in whitelist
    )
    blacklist = alpha_dir / "blacklist.txt"
    assert blacklist.exists()
    assert not any(
        line.strip() for line in blacklist.read_text(encoding="utf-8").splitlines()
    )


def test_w80_whitelist_is_w50_plus_exactly_30_extra_symbols():
    alphas_root = Path(__file__).resolve().parents[2]

    def read_whitelist(alpha_id: str) -> list[str]:
        return [
            line.strip()
            for line in (alphas_root / alpha_id / "whitelist.txt")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]

    w50 = read_whitelist("1d-vwaprev-w50-top15-p20")
    w80 = read_whitelist("1d-vwaprev-w80-top25-p15")

    assert set(w50) < set(w80)
    assert len(set(w80) - set(w50)) == 30


def test_vwaprev_topk_whitelists_match_docs_projects():
    # Lock the ported universes to the standalone docs projects' lists so a
    # future "sync all whitelists" pass cannot silently change them.
    repo_root = Path(__file__).resolve().parents[3]
    alphas_root = Path(__file__).resolve().parents[2]
    for alpha_id in ("1d-vwaprev-w50-top15-p20", "1d-vwaprev-w80-top25-p15"):
        docs_symbols = {
            line.strip()
            for line in (repo_root / "docs" / alpha_id / "whitelist.txt")
            .read_text()
            .splitlines()
            if line.strip()
        }
        alphas_symbols = {
            line.strip()
            for line in (alphas_root / alpha_id / "whitelist.txt")
            .read_text()
            .splitlines()
            if line.strip()
        }
        assert alphas_symbols == docs_symbols

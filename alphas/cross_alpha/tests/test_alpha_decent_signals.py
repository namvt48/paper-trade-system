from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from cross_alpha.spec import AlphaSpec
from cross_alpha.strategy import compute_signal_details, select_positions

# The 10 new 1d cross-sectional alphas ported from the "alpha-decent" backtest
# suite. The math tests re-derive each signal with plain pandas from the same
# synthetic panel and assert the engine's score matches exactly.
ALPHA_IDS = [
    "1d-demean-vol-times-std",
    "1d-kyle-lambda-zscore",
    "1d-momentum-minus-std",
    "1d-neg-std-close",
    "1d-range-volatility",
    "1d-rank-close-minus-std",
    "1d-residual-vol-chg",
    "1d-residual-vol-zscore",
    "1d-rvol-ratio-lvl",
    "1d-std-minus-ema",
]

SYMBOLS = [
    "BTCUSDT",
    "AUSDT",
    "BUSDT",
    "CUSDT",
    "DUSDT",
    "EUSDT",
    "FUSDT",
    "GUSDT",
]


def _make_panel(bars: int = 300, seed: int = 42) -> dict[str, pd.DataFrame]:
    """Deterministic synthetic daily panel: geometric-random-walk closes with
    independent lognormal volume and high/low envelopes."""
    rng = np.random.default_rng(seed)
    idx = pd.RangeIndex(bars)
    rets = rng.normal(0.001, 0.02, size=(bars, len(SYMBOLS)))
    close = pd.DataFrame(
        100.0 * np.exp(np.cumsum(rets, axis=0)), index=idx, columns=SYMBOLS
    )
    volume = pd.DataFrame(
        np.exp(rng.normal(10.0, 0.5, size=(bars, len(SYMBOLS)))),
        index=idx,
        columns=SYMBOLS,
    )
    half = np.abs(rng.normal(0.0, 0.01, size=(bars, len(SYMBOLS))))
    return {
        "close": close,
        "high": close * (1.0 + half),
        "low": close * (1.0 - half),
        "volume": volume,
        "quote_volume": close * volume,
    }


# ── reference math (mirrors the backtest operator definitions) ────────────
def _ts_std(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x.rolling(d, min_periods=max(2, d // 2)).std()


def _ts_mean(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x.rolling(d, min_periods=max(1, d // 2)).mean()


def _ts_zscore(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return (x - _ts_mean(x, d)) / _ts_std(x, d).replace(0, np.nan)


def _ts_momentum(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x / x.shift(d) - 1.0


def _ts_ema(x: pd.DataFrame, span: int) -> pd.DataFrame:
    return x.ewm(span=span, adjust=False, min_periods=max(1, span // 2)).mean()


def _cs_demean(x: pd.DataFrame) -> pd.DataFrame:
    return x.sub(x.mean(axis=1), axis=0)


def _cs_rank(x: pd.DataFrame) -> pd.DataFrame:
    return x.rank(axis=1, pct=True)


def _spec(
    signal: str,
    params: dict,
    universe_mode: str = "all",
    universe_size: int = 199,
    balanced: bool = False,
) -> AlphaSpec:
    return AlphaSpec(
        alpha_id=f"test-{signal}",
        timeframe="1d",
        signal=signal,
        params=params,
        universe_size=universe_size,
        universe_mode=universe_mode,
        rebalance_bars=1,
        vol_lookback=20,
        ppy=365,
        long_threshold=None,
        short_threshold=None,
        construction="winsor_cont",
        winsor_k=3.0,
        balanced=balanced,
    )


@pytest.mark.parametrize(
    ("signal", "params", "ref_fn"),
    [
        (
            "demean_vol_times_std",
            {"std_window": 20},
            lambda p: _cs_demean(p["volume"]) * _ts_std(p["close"], 20),
        ),
        (
            "kyle_lambda_zscore",
            {"abs_window": 20, "vol_window": 20, "z_window": 120},
            lambda p: _ts_zscore(
                p["close"]
                .pct_change(fill_method=None)
                .abs()
                .rolling(20, min_periods=10)
                .mean()
                / p["volume"].rolling(20, min_periods=10).mean().replace(0, np.nan),
                120,
            ),
        ),
        (
            "momentum_minus_std",
            {"momentum_window": 5, "std_window": 20},
            lambda p: _ts_momentum(p["close"], 5) - _ts_std(p["close"], 20),
        ),
        (
            "neg_std_close",
            {"std_window": 10},
            lambda p: -1.0 * _ts_std(p["close"], 10),
        ),
        (
            "range_volatility",
            {"std_window": 10},
            lambda p: -1.0 * _ts_std(p["high"] - p["low"], 10),
        ),
        (
            "rank_close_minus_std",
            {"std_window": 20},
            lambda p: _cs_rank(p["close"]) - _ts_std(p["close"], 20),
        ),
        (
            "rvol_ratio_lvl",
            {"short_window": 5, "long_window": 60},
            lambda p: (
                -1.0
                * (
                    p["volume"].rolling(5, min_periods=3).mean()
                    / p["volume"].rolling(60, min_periods=30).mean().replace(0, np.nan)
                )
            ),
        ),
        (
            "std_minus_ema",
            {"std_window": 20, "ema_span": 20},
            lambda p: _ts_std(p["close"], 20) - _ts_ema(p["close"], 20),
        ),
    ],
)
def test_new_signal_math_matches_reference(signal: str, params: dict, ref_fn):
    panel = _make_panel()
    spec = _spec(signal, params)
    score, _, _, _ = compute_signal_details(panel, spec)
    assert score is not None
    expected = ref_fn(panel)
    last = score.iloc[-1]
    exp_last = expected.iloc[-1]
    for sym in SYMBOLS:
        if np.isnan(exp_last[sym]):
            assert np.isnan(last[sym])
        else:
            assert last[sym] == pytest.approx(exp_last[sym], rel=1e-9, abs=1e-12)


@pytest.mark.parametrize("seed", [1, 7, 123])
def test_residual_vol_chg_matches_reference(seed: int):
    panel = _make_panel(seed=seed)
    ret = panel["close"].pct_change(fill_method=None)
    btc = ret["BTCUSDT"]
    bench = pd.DataFrame({c: btc for c in ret.columns}, index=ret.index)
    cov = ret.rolling(60, min_periods=30).cov(bench)
    var = bench.rolling(60, min_periods=30).var().replace(0, np.nan)
    beta = cov.div(var, axis=0)
    # pandas 3.0 `df * series` aligns on columns, not rows — mul(axis=0) matches the backtest.
    resid = ret.sub(beta.mul(btc, axis=0))
    res_vol = resid.rolling(20, min_periods=10).std()
    expected = -1.0 * (res_vol - res_vol.shift(60))

    spec = _spec(
        "residual_vol_chg",
        {"beta_window": 60, "resid_window": 20, "chg_window": 60},
    )
    score, _, _, indicators = compute_signal_details(panel, spec)
    assert score is not None
    last = score.iloc[-1]
    exp_last = expected.iloc[-1]
    for sym in SYMBOLS:
        if np.isnan(exp_last[sym]):
            assert np.isnan(last[sym])
        else:
            assert last[sym] == pytest.approx(exp_last[sym], rel=1e-9, abs=1e-12)
    assert "residual_vol_chg" in indicators
    assert "beta" in indicators
    assert "residual_vol" in indicators


@pytest.mark.parametrize("seed", [2, 8, 99])
def test_residual_vol_zscore_matches_reference(seed: int):
    panel = _make_panel(seed=seed)
    ret = panel["close"].pct_change(fill_method=None)
    btc = ret["BTCUSDT"]
    bench = pd.DataFrame({c: btc for c in ret.columns}, index=ret.index)
    cov = ret.rolling(60, min_periods=30).cov(bench)
    var = bench.rolling(60, min_periods=30).var().replace(0, np.nan)
    beta = cov.div(var, axis=0)
    resid = ret.sub(beta.mul(btc, axis=0))
    res_vol = resid.rolling(20, min_periods=10).std()
    expected = -1.0 * _ts_zscore(res_vol, 120)

    spec = _spec(
        "residual_vol_zscore",
        {"beta_window": 60, "resid_window": 20, "z_window": 120},
    )
    score, _, _, _ = compute_signal_details(panel, spec)
    assert score is not None
    last = score.iloc[-1]
    exp_last = expected.iloc[-1]
    for sym in SYMBOLS:
        if np.isnan(exp_last[sym]):
            assert np.isnan(last[sym])
        else:
            assert last[sym] == pytest.approx(exp_last[sym], rel=1e-9, abs=1e-12)


def test_missing_benchmark_falls_back_to_mean():
    panel = _make_panel()
    panel["close"] = panel["close"].drop(columns=["BTCUSDT"])
    panel["high"] = panel["high"].drop(columns=["BTCUSDT"])
    panel["low"] = panel["low"].drop(columns=["BTCUSDT"])
    panel["volume"] = panel["volume"].drop(columns=["BTCUSDT"])
    panel["quote_volume"] = panel["quote_volume"].drop(columns=["BTCUSDT"])

    ret = panel["close"].pct_change(fill_method=None)
    btc = ret.mean(axis=1)
    bench = pd.DataFrame({c: btc for c in ret.columns}, index=ret.index)
    beta = (
        ret.rolling(60, min_periods=30)
        .cov(bench)
        .div(bench.rolling(60, min_periods=30).var().replace(0, np.nan), axis=0)
    )
    res_vol = ret.sub(beta.mul(btc, axis=0)).rolling(20, min_periods=10).std()
    expected = -1.0 * (res_vol - res_vol.shift(60))

    spec = _spec(
        "residual_vol_chg",
        {"beta_window": 60, "resid_window": 20, "chg_window": 60},
    )
    score, _, _, _ = compute_signal_details(panel, spec)
    last = score.iloc[-1]
    exp_last = expected.iloc[-1]
    syms = [s for s in SYMBOLS if s != "BTCUSDT"]
    for sym in syms:
        assert last[sym] == pytest.approx(exp_last[sym], rel=1e-9, abs=1e-12)


def test_balanced_false_keeps_uneven_sides_winsor_cont():
    # A strongly long-skewed cross-section: neg_std_close against a panel
    # where most symbols are flat (low std) and one is volatile.
    idx = pd.RangeIndex(30)
    close = pd.DataFrame({s: 100.0 for s in SYMBOLS}, index=idx)
    close["AUSDT"] = 100.0 + np.linspace(0, 2, 30)  # steady trend, modest std
    close["BTCUSDT"] = 100.0 + 30.0 * np.sin(np.linspace(0, 20, 30))  # very volatile

    panel = {
        "close": close,
        "high": close * 1.01,
        "low": close * 0.99,
        "volume": pd.DataFrame(1000.0, index=idx, columns=SYMBOLS),
        "quote_volume": close * 1000.0,
    }
    spec = _spec("neg_std_close", {"std_window": 10}, balanced=False)
    selection = select_positions(panel, spec)

    assert selection.longs and selection.shorts
    assert len(selection.longs) != len(selection.shorts)
    assert "BTCUSDT" in selection.shorts


def test_balanced_true_default_trims_to_even_sides():
    idx = pd.RangeIndex(30)
    close = pd.DataFrame({s: 100.0 for s in SYMBOLS}, index=idx)
    close["AUSDT"] = 100.0 + np.linspace(0, 2, 30)
    close["BTCUSDT"] = 100.0 + 30.0 * np.sin(np.linspace(0, 20, 30))

    panel = {
        "close": close,
        "high": close * 1.01,
        "low": close * 0.99,
        "volume": pd.DataFrame(1000.0, index=idx, columns=SYMBOLS),
        "quote_volume": close * 1000.0,
    }
    spec = _spec("neg_std_close", {"std_window": 10}, balanced=True)
    selection = select_positions(panel, spec)

    assert selection.longs and selection.shorts
    assert len(selection.longs) == len(selection.shorts)
    assert selection.diagnostics["gross"] == pytest.approx(1.0)


@pytest.mark.parametrize("alpha_id", ALPHA_IDS)
def test_new_alpha_spec_files_are_complete(alpha_id: str):
    alphas_root = Path(__file__).resolve().parents[2]
    alpha_dir = alphas_root / alpha_id

    spec = AlphaSpec.load(alpha_dir / "spec.json")
    assert spec.timeframe == "1d"
    assert spec.universe_mode == "dynamic_top_k"
    assert spec.construction == "winsor_cont"
    assert spec.winsor_k == 3.0
    assert spec.balanced is False
    assert spec.reverse is False
    assert spec.required_bars > 0

    whitelist = [
        line.strip()
        for line in (alpha_dir / "whitelist.txt").read_text().splitlines()
        if line.strip()
    ]
    assert len(whitelist) == 199
    assert len(set(whitelist)) == 199
    assert all(s.endswith("USDT") and s == s.upper() for s in whitelist)

    blacklist = (alpha_dir / "blacklist.txt").read_text().strip()
    assert blacklist == ""


@pytest.mark.parametrize(
    ("alpha_id", "universe_size", "signal"),
    [
        ("1d-demean-vol-times-std", 199, "demean_vol_times_std"),
        ("1d-kyle-lambda-zscore", 100, "kyle_lambda_zscore"),
        ("1d-momentum-minus-std", 199, "momentum_minus_std"),
        ("1d-neg-std-close", 199, "neg_std_close"),
        ("1d-range-volatility", 199, "range_volatility"),
        ("1d-rank-close-minus-std", 199, "rank_close_minus_std"),
        ("1d-residual-vol-chg", 100, "residual_vol_chg"),
        ("1d-residual-vol-zscore", 100, "residual_vol_zscore"),
        ("1d-rvol-ratio-lvl", 100, "rvol_ratio_lvl"),
        ("1d-std-minus-ema", 199, "std_minus_ema"),
    ],
)
def test_new_alpha_spec_fields(alpha_id: str, universe_size: int, signal: str):
    alphas_root = Path(__file__).resolve().parents[2]
    spec = AlphaSpec.load(alphas_root / alpha_id / "spec.json")
    assert spec.alpha_id == alpha_id
    assert spec.signal == signal
    assert spec.universe_size == universe_size
    assert spec.rebalance_bars == 1
    assert spec.vol_lookback == 20
    assert spec.ppy == 365
    assert spec.target_vol == pytest.approx(0.10)
    assert spec.max_leverage == pytest.approx(3.0)
    assert spec.fee_bps == pytest.approx(7.0)
    assert spec.top_k is None


def test_dynamic_topk_100_filter_restricts_to_liquid_symbols():
    # dynamic_top_k size 5 must exclude the illiquid symbol on the final bar
    # while scoring the same numbers on the survivors.
    panel = _make_panel(seed=5)
    panel["volume"]["GUSDT"] = 1e-6  # illiquid
    panel["quote_volume"] = panel["close"] * panel["volume"]

    spec = _spec(
        "neg_std_close",
        {"std_window": 10},
        universe_mode="dynamic_top_k",
        universe_size=5,
        balanced=False,
    )
    score, _, _, _ = compute_signal_details(panel, spec)
    last = score.iloc[-1].dropna()
    assert "GUSDT" not in last.index
    assert len(last) == 5
    liq_rank = (
        (panel["close"] * panel["volume"])
        .rolling(30, min_periods=1)
        .mean()
        .rank(axis=1, ascending=False)
    )
    assert set(last.index) == set(liq_rank.iloc[-1][liq_rank.iloc[-1] <= 5].index)

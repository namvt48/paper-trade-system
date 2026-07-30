"""Correctness coverage for XauM30RunnerStrategy's actual trading logic --
entry signal, stop placement, and position sizing had zero test coverage
before this file (test_xau_m30.py only covers the M30 candle builder).
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from runner.strategies.xau_m30.strategy import (
    PRESETS,
    CandleSeries,
    XauM30RunnerStrategy,
    _atr,
    _ema,
    _rsi,
)


def _build_pullback_series(
    n=220,
    trend_slope=0.1,
    osc_amp=2.0,
    osc_period=14,
    pb_len=3,
    pb_drift=-0.6,
    pb_wick=3.0,
    turn_drift=1.5,
    wick=0.6,
    body=0.3,
    start=2000.0,
) -> CandleSeries:
    """A trending-with-oscillation price path whose final 4 bars are a shallow
    pullback (RSI recycles into the 45-55 zone) followed by a green turn-up
    bar that closes back above EMA9 -- exactly preset 4/5/6's "pullback
    continuation" setup. Parameters were found by grid search against the
    real _matches() implementation, not hand-derived from the formula.
    """
    closes = [start]
    for i in range(1, n):
        if i < n - pb_len - 1:
            base = trend_slope + osc_amp * (
                math.sin(2 * math.pi * i / osc_period)
                - math.sin(2 * math.pi * (i - 1) / osc_period)
            )
            closes.append(closes[-1] + base)
        elif i < n - 1:
            closes.append(closes[-1] + pb_drift)
        else:
            closes.append(closes[-1] + turn_drift)
    opens = [c - body for c in closes]
    lows, highs = [], []
    for idx, (o, c) in enumerate(zip(opens, closes)):
        w = pb_wick if (n - pb_len - 1) <= idx < n else wick
        highs.append(max(o, c) + wick)
        lows.append(min(o, c) - w)
    times = [i * 30 * 60 * 1000 for i in range(n)]
    return CandleSeries(
        tuple(opens), tuple(highs), tuple(lows), tuple(closes), tuple(times)
    )


def _mirror(series: CandleSeries, pivot: float = 3000.0) -> CandleSeries:
    """Reflect a price series around `pivot` (price' = 2*pivot - price, with
    high/low swapped) -- turns an uptrend fixture into a bit-for-bit
    symmetric downtrend, since EMA is linear under negation and RSI is
    exactly complementary (RSI(mirrored) == 100 - RSI(original))."""
    opens = tuple(2 * pivot - o for o in series.opens)
    closes = tuple(2 * pivot - c for c in series.closes)
    highs = tuple(2 * pivot - lo for lo in series.lows)
    lows = tuple(2 * pivot - hi for hi in series.highs)
    return CandleSeries(opens, highs, lows, closes, series.times)


def _flat_h4(n=60, level=2000.0) -> CandleSeries:
    times = tuple(i * 4 * 3600 * 1000 for i in range(n))
    return CandleSeries(
        (level,) * n, (level + 1.0,) * n, (level - 1.0,) * n, (level,) * n, times
    )


def _trending_h4(n=60, start=2000.0, step=3.0) -> CandleSeries:
    closes = tuple(start + i * step for i in range(n))
    opens = tuple(c - 1.0 for c in closes)
    highs = tuple(c + 1.5 for c in closes)
    lows = tuple(c - 1.5 for c in closes)
    times = tuple(i * 4 * 3600 * 1000 for i in range(n))
    return CandleSeries(opens, highs, lows, closes, times)


def _strategy(preset_number: int) -> XauM30RunnerStrategy:
    strategy = object.__new__(XauM30RunnerStrategy)
    strategy.preset = PRESETS[preset_number]
    return strategy


def test_matches_fires_long_on_pullback_continuation_preset4() -> None:
    series = _build_pullback_series()
    e9, e21, e50 = (
        _ema(series.closes, 9),
        _ema(series.closes, 21),
        _ema(series.closes, 50),
    )
    rsi_values = _rsi(series.closes)
    atr_values = _atr(series)
    strategy = _strategy(4)

    assert (
        strategy._matches("LONG", series, e9, e21, e50, rsi_values, atr_values) is True
    )
    assert (
        strategy._matches("SHORT", series, e9, e21, e50, rsi_values, atr_values)
        is False
    )


def test_matches_fires_short_on_mirrored_pullback_preset4() -> None:
    # Same fixture, reflected -- proves the SHORT branch is independently
    # correct and directionally exclusive, not just "the LONG branch happens
    # to be right."
    series = _mirror(_build_pullback_series())
    e9, e21, e50 = (
        _ema(series.closes, 9),
        _ema(series.closes, 21),
        _ema(series.closes, 50),
    )
    rsi_values = _rsi(series.closes)
    atr_values = _atr(series)
    strategy = _strategy(4)

    assert (
        strategy._matches("SHORT", series, e9, e21, e50, rsi_values, atr_values) is True
    )
    assert (
        strategy._matches("LONG", series, e9, e21, e50, rsi_values, atr_values) is False
    )


def test_entry_side_requires_unambiguous_macro_match_for_gated_presets() -> None:
    # Reference (alpha_logic_bundle/backtest_engine.py:307-309: `uptrend &
    # (h4_trend == 1)`; alpha_11.py:44-51: `macro == 1`) requires an
    # UNAMBIGUOUS H4 match, not merely "H4 doesn't disagree". A neutral H4
    # (macro=None) must block BOTH sides for every macro_gated preset.
    series = _build_pullback_series()
    strategy = _strategy(4)

    assert strategy._entry_side(series, _trending_h4(step=3.0)) == "LONG"
    assert strategy._entry_side(series, _flat_h4()) is None
    assert strategy._entry_side(series, _trending_h4(step=-3.0)) is None


def test_entry_side_ignores_h4_entirely_for_non_macro_gated_preset() -> None:
    # Preset 10 has macro_gated=False -- it must fire off the M30 signal
    # alone regardless of what H4 is doing (matches Alpha 10 spec: "không
    # dùng H4").
    series = _build_pullback_series()
    strategy = _strategy(10)
    assert strategy.preset.macro_gated is False

    with_neutral_h4 = strategy._entry_side(series, _flat_h4())
    with_disagreeing_h4 = strategy._entry_side(series, _trending_h4(step=-3.0))

    assert with_neutral_h4 == with_disagreeing_h4


def test_stop_price_structure_based_uses_low_and_ema50_with_buffer() -> None:
    # A perfectly flat series: EMA50 == 2000.0 exactly, ATR == 10.0 exactly
    # (constant true range), so the stop is hand-verifiable:
    # raw = min(low=1995, ema50=2000) - 1.5*atr(10) = 1995 - 15 = 1980.0.
    n = 15
    series = CandleSeries(
        (2000.0,) * n, (2005.0,) * n, (1995.0,) * n, (2000.0,) * n, tuple(range(n))
    )
    atr_value = _atr(series)[-1]
    strategy = _strategy(4)

    assert atr_value == pytest.approx(10.0)
    assert strategy._stop_price("LONG", 2000.0, series, atr_value) == pytest.approx(
        1980.0
    )
    assert strategy._stop_price("SHORT", 2000.0, series, atr_value) == pytest.approx(
        2020.0
    )


def test_stop_price_pure_atr_uses_per_preset_multiplier() -> None:
    n = 15
    series = CandleSeries(
        (2000.0,) * n, (2005.0,) * n, (1995.0,) * n, (2000.0,) * n, tuple(range(n))
    )

    assert _strategy(10)._stop_price("LONG", 2000.0, series, 5.0) == pytest.approx(
        2000.0 - 1.2 * 5.0
    )
    assert _strategy(11)._stop_price("SHORT", 2000.0, series, 5.0) == pytest.approx(
        2000.0 + 1.6 * 5.0
    )
    assert _strategy(12)._stop_price("LONG", 2000.0, series, 5.0) == pytest.approx(
        2000.0 - 1.35 * 5.0
    )


@pytest.mark.asyncio
async def test_open_if_allowed_sizes_qty_from_risk_pct_and_caps_by_leverage() -> None:
    series = _build_pullback_series()
    strategy = _strategy(4)
    strategy.alpha_id = "xau-m30-alpha-4"
    strategy.version = "1"
    strategy.symbol = "XAUUSDT"
    strategy.exchange = "binance"
    strategy.fee_pct = 0.0005
    strategy.capital = 10_000.0
    strategy.risk_pct = 0.0025
    strategy.leverage = 10.0
    strategy._positions = {}
    strategy.ctx = SimpleNamespace(emit_signal=AsyncMock(return_value={"ok": True}))

    await strategy._open_if_allowed("LONG", series)

    assert strategy.ctx.emit_signal.await_count == 1
    call = strategy.ctx.emit_signal.await_args
    assert call.args[0] == "OPEN"
    entry = series.closes[-1]
    sl = strategy._stop_price("LONG", entry, series, _atr(series)[-1])
    risk = abs(entry - sl)
    expected_qty = math.floor((10_000.0 * 0.0025 / risk) * 1000) / 1000
    expected_qty = min(
        expected_qty, math.floor((10_000.0 * 10.0 / entry) * 1000) / 1000
    )
    assert call.kwargs["qty"] == pytest.approx(expected_qty)
    assert call.kwargs["side"] == "LONG"
    assert len(strategy._positions) == 1


@pytest.mark.asyncio
async def test_open_if_allowed_skips_when_risk_too_small_for_min_qty() -> None:
    # An entry with essentially zero stop distance (flat series -> ATR==0)
    # must be skipped, not divide-by-zero or open a runaway position.
    n = 15
    series = CandleSeries(
        (2000.0,) * n, (2000.0,) * n, (2000.0,) * n, (2000.0,) * n, tuple(range(n))
    )
    strategy = _strategy(4)
    strategy._positions = {}
    strategy.capital = 10_000.0
    strategy.risk_pct = 0.0025
    strategy.leverage = 10.0
    strategy.ctx = SimpleNamespace(emit_signal=AsyncMock())

    await strategy._open_if_allowed("LONG", series)

    strategy.ctx.emit_signal.assert_not_awaited()
    assert strategy._positions == {}


@pytest.mark.asyncio
async def test_open_if_allowed_preset4_blocks_pyramid_add_at_worse_price() -> None:
    # Reference (alpha_logic_bundle/strategies/alpha_4.py
    # check_pyramiding_safety) only allows adding to a LONG stack at a price
    # >= every existing leg's entry. An existing LONG well above the new
    # signal's entry (a worse add) must still be blocked after the
    # strict-vs-inclusive boundary fix -- this is the actually observable
    # half of that fix (the exact-equal-price boundary is unreachable in
    # practice: it always falls inside the earlier 0.5*ATR minimum-spacing
    # guard, which blocks it regardless of this check).
    n = 15
    series = CandleSeries(
        (2000.0,) * n, (2005.0,) * n, (1995.0,) * n, (2000.0,) * n, tuple(range(n))
    )
    atr = _atr(series)[-1]
    strategy = _strategy(4)
    strategy.alpha_id = "xau-m30-alpha-4"
    strategy.version = "1"
    strategy.symbol = "XAUUSDT"
    strategy.exchange = "binance"
    strategy.fee_pct = 0.0005
    strategy.capital = 10_000.0
    strategy.risk_pct = 0.0025
    strategy.leverage = 10.0
    strategy._positions = {
        "p1": {
            "position_id": "p1",
            "symbol": "XAUUSDT",
            "side": "LONG",
            "entry": series.closes[-1] + 5 * atr,
            "qty": 1.0,
            "tp": 0.0,
            "sl": 0.0,
            "initial_sl": 0.0,
            "breakeven": False,
        }
    }
    strategy.ctx = SimpleNamespace(emit_signal=AsyncMock())

    await strategy._open_if_allowed("LONG", series)

    strategy.ctx.emit_signal.assert_not_awaited()
    assert len(strategy._positions) == 1

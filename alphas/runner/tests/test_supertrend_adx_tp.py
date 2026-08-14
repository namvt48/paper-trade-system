"""Correctness coverage for the supertrend-adx-tp runner strategy.

Reference: docs/supertrend adx tp - papertrade.py

Semantics:
  - Supertrend(3,10) on closed 15m candles drives entries: a flip to
    downtrend (1) opens SHORT, a flip to uptrend (-1) opens LONG.
  - DMI/ADX(14,14) is evaluated on the same closed 15m candles. A confirmed
    ADX pivot high (pivot_left=4, pivot_right=2) is known only after the
    confirmation bar closes.
  - The second, lower ADX peak in the same Supertrend cycle closes the
    position with reason ADX_LOWER_PEAK_TP. A cycle emits at most one ADX TP.
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from runner.strategies.supertrend_adx_tp.strategy import (
    DOWNTREND,
    M15_MS,
    UPTREND,
    CandleSeries,
    SupertrendAdxTpRunnerStrategy,
    compute_dmi_adx,
    compute_supertrend_directions,
    confirmed_pivot_high,
)

_BASE_MS = 1_786_000_000_000  # realistic ms epoch > 1e12 (not treated as seconds)
_N_BARS = 100  # well above min_m15_bars=80 required by _scan_15m


# ------------------------------------------------------------- indicator math


def _series(OHLC: list[tuple[float, float, float, float]]) -> CandleSeries:
    opens = [_o for _o, _h, _l, _c in OHLC]
    highs = [_h for _o, _h, _l, _c in OHLC]
    lows = [_l for _o, _h, _l, _c in OHLC]
    closes = [_c for _o, _h, _l, _c in OHLC]
    times = [_BASE_MS + i * M15_MS for i in range(len(OHLC))]
    return CandleSeries(
        tuple(opens), tuple(highs), tuple(lows), tuple(closes), tuple(times)
    )


def _rising_closes(n: int = _N_BARS) -> list[tuple[float, float, float, float]]:
    return [
        (
            2000 + i * 0.5 - 0.2,
            2000 + i * 0.5 + 0.4,
            2000 + i * 0.5 - 0.4,
            2000 + i * 0.5,
        )
        for i in range(n)
    ]


def _falling_closes(n: int = _N_BARS) -> list[tuple[float, float, float, float]]:
    return [
        (
            2000 - i * 0.5 - 0.2,
            2000 - i * 0.5 + 0.4,
            2000 - i * 0.5 - 0.4,
            2000 - i * 0.5,
        )
        for i in range(n)
    ]


def test_supertrend_rising_is_uptrend() -> None:
    s = _series(_rising_closes())
    directions = compute_supertrend_directions(s.highs, s.lows, s.closes)
    assert directions[-1] == UPTREND


def test_supertrend_falling_is_downtrend() -> None:
    s = _series(_falling_closes())
    directions = compute_supertrend_directions(s.highs, s.lows, s.closes)
    assert directions[-1] == DOWNTREND


def test_dmi_adx_returns_finite_series() -> None:
    s = _series(_rising_closes(80))
    plus_di, minus_di, adx = compute_dmi_adx(s.highs, s.lows, s.closes)
    assert len(adx) == 80
    # ADX warms up after di_length + adx_smoothing bars; tail must be finite.
    assert all(math.isfinite(v) for v in adx[-5:])
    assert all(math.isfinite(v) for v in plus_di[-5:])
    assert all(math.isfinite(v) for v in minus_di[-5:])


def test_confirmed_pivot_high_requires_full_left_window() -> None:
    # A peak at index 3 with left=4 cannot be confirmed (not enough left bars).
    values = [10.0, 11.0, 12.0, 30.0, 11.0, 10.0, 9.0]
    assert confirmed_pivot_high(values, confirmation_index=6, left=4, right=2) is None


def test_confirmed_pivot_high_confirms_after_right_bars() -> None:
    # Peak is at index 4 (value 15.0). With right=2 it is confirmed only once
    # the confirmation bar reaches index 6 (pivot_index = 6 - 2 = 4).
    values = [1.0, 2.0, 3.0, 4.0, 15.0, 3.0, 2.0, 1.0, 1.0, 1.0]
    # Before confirmation: pivot_index = 5 - 2 = 3, not the peak -> None.
    assert confirmed_pivot_high(values, confirmation_index=5, left=4, right=2) is None
    # At confirmation: pivot_index = 6 - 2 = 4 -> the peak, confirmed.
    assert confirmed_pivot_high(values, confirmation_index=6, left=4, right=2) == (
        4,
        15.0,
    )


# ------------------------------------------------------------------ wiring


def _strategy(ctx=None) -> SupertrendAdxTpRunnerStrategy:
    strategy = object.__new__(SupertrendAdxTpRunnerStrategy)
    strategy.factor = 3.0
    strategy.atr_period = 10
    strategy.di_length = 14
    strategy.adx_smoothing = 14
    strategy.pivot_left = 4
    strategy.pivot_right = 2
    strategy.min_peak_drop = 0.0
    strategy.symbol = "PAXGUSDT"
    strategy.exchange = "binance"
    strategy.capital = 10_000.0
    strategy.leverage = 10.0
    strategy.position_fraction = 1.0
    strategy.fee_pct = 0.0005
    strategy.m15_warmup_bars = 320
    strategy.retain_bars = 320
    strategy.min_m15_bars = 80
    strategy.timestamp_semantics = "open"
    strategy.alpha_id = "supertrend-adx-tp"
    strategy.version = "1"
    strategy._last_trend_dir = 0
    strategy._last_15m_open = None
    strategy._pending_15m_open = None
    strategy._positions = {}
    if ctx is None:
        ctx = SimpleNamespace(
            emit_signal=AsyncMock(),
            load_authoritative_positions=lambda: None,
            load_positions=lambda: None,
            state=SimpleNamespace(ready=True),
            price_alerts=None,
            cache=SimpleNamespace(),
        )
    strategy.ctx = ctx
    return strategy


def _cache_with(series: CandleSeries) -> SimpleNamespace:
    class _Cache:
        def snapshot(self, symbol, tf, bars):
            return SimpleNamespace(
                opens=series.opens,
                highs=series.highs,
                lows=series.lows,
                closes=series.closes,
                times=series.times,
            )

    return SimpleNamespace(snapshot=_Cache().snapshot)


def test_requests_15m_channel() -> None:
    assert SupertrendAdxTpRunnerStrategy.get_required_channels(
        {"symbol": "PAXGUSDT"}
    ) == ["kline:15m"]


def test_warmup_tfs_and_bars() -> None:
    strategy = _strategy()
    assert strategy.get_warmup_symbols() == ["PAXGUSDT"]
    assert strategy.get_warmup_tfs() == ["15m"]
    assert strategy.get_warmup_bars("15m") == 320


def test_registry_registers_strategy() -> None:
    from runner.strategy.registry import StrategyRegistry

    from runner.strategies.supertrend_adx_tp import register

    registry = StrategyRegistry()
    register(registry)
    assert "supertrend_adx_tp" in registry.names()
    assert registry.get_class("supertrend_adx_tp") is SupertrendAdxTpRunnerStrategy


# ------------------------------------------------------------- entry on flip


@pytest.mark.asyncio
async def test_15m_flip_to_uptrend_opens_long() -> None:
    series = _series(_rising_closes())
    ctx = SimpleNamespace(
        emit_signal=AsyncMock(return_value={"ok": True}),
        load_authoritative_positions=lambda: None,
        load_positions=lambda: None,
        state=SimpleNamespace(ready=True),
        price_alerts=None,
        cache=_cache_with(series),
    )
    strategy = _strategy(ctx)
    strategy._last_trend_dir = DOWNTREND
    strategy._last_15m_open = series.times[-1] - M15_MS
    await strategy._scan_15m(series.times[-1])

    assert ctx.emit_signal.await_count == 1
    call = ctx.emit_signal.await_args
    assert call.args[0] == "OPEN"
    assert call.kwargs["side"] == "LONG"
    assert call.kwargs["symbol"] == "PAXGUSDT"
    assert len(strategy._positions) == 1


@pytest.mark.asyncio
async def test_15m_flip_to_downtrend_opens_short() -> None:
    series = _series(_falling_closes())
    ctx = SimpleNamespace(
        emit_signal=AsyncMock(return_value={"ok": True}),
        load_authoritative_positions=lambda: None,
        load_positions=lambda: None,
        state=SimpleNamespace(ready=True),
        price_alerts=None,
        cache=_cache_with(series),
    )
    strategy = _strategy(ctx)
    strategy._last_trend_dir = UPTREND
    strategy._last_15m_open = series.times[-1] - M15_MS
    await strategy._scan_15m(series.times[-1])

    assert ctx.emit_signal.await_count == 1
    call = ctx.emit_signal.await_args
    assert call.args[0] == "OPEN"
    assert call.kwargs["side"] == "SHORT"


# ------------------------------------------------------- trend flip closes pos


@pytest.mark.asyncio
async def test_trend_flip_closes_opposite_position() -> None:
    series = _series(_rising_closes())  # uptrend
    ctx = SimpleNamespace(
        emit_signal=AsyncMock(return_value={"ok": True}),
        load_authoritative_positions=lambda: None,
        load_positions=lambda: None,
        state=SimpleNamespace(ready=True),
        price_alerts=None,
        cache=_cache_with(series),
    )
    strategy = _strategy(ctx)
    strategy._last_trend_dir = DOWNTREND
    strategy._last_15m_open = series.times[-1] - M15_MS
    strategy._positions = {
        "p1": {
            "position_id": "p1",
            "symbol": "PAXGUSDT",
            "side": "SHORT",
            "entry": 2600.0,
            "qty": 1.0,
            "previous_adx_peak": None,
            "tp_triggered": False,
        }
    }
    await strategy._scan_15m(series.times[-1])

    reasons = [c.args[0] for c in ctx.emit_signal.await_args_list]
    assert reasons == ["CLOSE", "OPEN"]
    assert ctx.emit_signal.await_args_list[0].kwargs["reason"] == "TREND_FLIP"
    assert ctx.emit_signal.await_args_list[1].kwargs["side"] == "LONG"
    assert len(strategy._positions) == 1


# ------------------------------------------------- ADX lower-peak TP (ADX is
# monkeypatched so the pivot is deterministic across scans)


@pytest.mark.asyncio
async def test_adx_lower_peak_tp_closes_position(monkeypatch) -> None:
    import runner.strategies.supertrend_adx_tp.strategy as mod

    OHLC = _rising_closes()
    series = _series(OHLC)
    ctx = SimpleNamespace(
        emit_signal=AsyncMock(return_value={"ok": True}),
        load_authoritative_positions=lambda: None,
        load_positions=lambda: None,
        state=SimpleNamespace(ready=True),
        price_alerts=None,
        cache=_cache_with(series),
    )
    strategy = _strategy(ctx)
    strategy._last_trend_dir = UPTREND
    strategy._last_15m_open = series.times[-1] - M15_MS
    strategy._positions = {
        "p1": {
            "position_id": "p1",
            "symbol": "PAXGUSDT",
            "side": "LONG",
            "entry": 2000.0,
            "qty": 1.0,
            "previous_adx_peak": None,
            "tp_triggered": False,
        }
    }

    # The scan feeds the CLOSED prefix (series[:-1]) to compute_dmi_adx. Its
    # confirmed pivot for a length-L prefix sits at index L - 1 - pivot_right
    # (pivot_index = confirmation_index - right, confirmation_index = L-1).
    prefix_len = len(OHLC) - 1
    peak_idx = prefix_len - 1 - strategy.pivot_right

    adx_first = [10.0] * prefix_len
    adx_first[peak_idx] = 30.0
    monkeypatch.setattr(
        mod,
        "compute_dmi_adx",
        lambda highs, lows, closes, di_length, adx_smoothing: ([], [], adx_first),
    )
    await strategy._scan_15m(series.times[-1])
    # First scan only records the peak; no close yet.
    assert strategy._positions["p1"]["previous_adx_peak"] == 30.0
    assert ctx.emit_signal.await_count == 0

    # Second scan: a lower second peak in the same cycle closes the position.
    # Advance to the next candle open (new series), same trend.
    OHLC2 = _rising_closes() + [
        (
            2000 + _N_BARS * 0.5 - 0.2,
            2000 + _N_BARS * 0.5 + 0.4,
            2000 + _N_BARS * 0.5 - 0.4,
            2000 + _N_BARS * 0.5,
        )
    ]
    series2 = _series(OHLC2)
    ctx.cache = _cache_with(series2)
    prefix_len2 = len(OHLC2) - 1
    peak_idx2 = prefix_len2 - 1 - strategy.pivot_right
    adx_second = [10.0] * prefix_len2
    adx_second[peak_idx2] = 20.0  # lower than 30 -> TP
    monkeypatch.setattr(
        mod,
        "compute_dmi_adx",
        lambda highs, lows, closes, di_length, adx_smoothing: ([], [], adx_second),
    )
    strategy._last_15m_open = series.times[-1]
    await strategy._scan_15m(series2.times[-1])

    assert ctx.emit_signal.await_count == 1
    call = ctx.emit_signal.await_args
    assert call.args[0] == "CLOSE"
    assert call.kwargs["reason"] == "ADX_LOWER_PEAK_TP"
    assert strategy._positions == {}

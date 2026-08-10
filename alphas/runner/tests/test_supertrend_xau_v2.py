"""Correctness coverage for the supertrend-xau-v2 runner strategy.

V2 semantics (docs/alpha_supertrend_trailing_5pct_v2.py / _10pct_v2.py):
  - Supertrend(3,10) on RAW 15m candles drives entries (flip -> open).
  - Trailing TP is simulated minute-by-minute on 1m candles with a strict
    causal order: gap at open -> previous-minute trailing vs low/high -> only
    if not exited, update peak and compute next trailing (effective next min).
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from runner.strategies.supertrend_xau_v2.strategy import (
    DOWNTREND,
    M15_MS,
    UPTREND,
    CandleSeries,
    SupertrendXauV2RunnerStrategy,
    process_long_minute,
    process_short_minute,
)


# ------------------------------------------------------- 1m trailing causality


def test_long_gap_down_at_open_exits_at_open_minus_slippage() -> None:
    # Active trailing 104.75; minute opens at 104.5 (gap through) -> exit at
    # open * (1 - slippage).
    exited, exit_price, best, nxt = process_long_minute(
        minute_open=104.5,
        minute_high=105.0,
        minute_low=104.4,
        entry_price=100.0,
        best_high=105.0,
        active_trailing=104.75,
        retrace_pct=0.05,
    )
    assert exited
    assert exit_price == pytest.approx(104.5 * (1 - 0.0001))
    assert nxt is None


def test_long_touch_trailing_exits_at_trailing_minus_slippage() -> None:
    # Open above trailing but low pierces it -> exit at trailing*(1-slippage).
    exited, exit_price, best, nxt = process_long_minute(
        minute_open=105.2,
        minute_high=105.3,
        minute_low=104.7,
        entry_price=100.0,
        best_high=105.0,
        active_trailing=104.75,
        retrace_pct=0.05,
    )
    assert exited
    assert exit_price == pytest.approx(104.75 * (1 - 0.0001))
    assert nxt is None


def test_long_updates_peak_and_trailing_only_when_not_exited() -> None:
    # No active trailing yet, new high 108 -> peak profit 8, retained 95% ->
    # trailing = 100 + 7.6 = 107.6, effective NEXT minute.
    exited, exit_price, best, nxt = process_long_minute(
        minute_open=106.0,
        minute_high=108.0,
        minute_low=105.5,
        entry_price=100.0,
        best_high=105.0,
        active_trailing=None,
        retrace_pct=0.05,
    )
    assert not exited
    assert exit_price is None
    assert best == pytest.approx(108.0)
    assert nxt == pytest.approx(107.6)


def test_long_trailing_ratchets_up_only() -> None:
    # Active trailing 107.6; new high 110 (peak 10) -> calculated 109.5, so
    # trailing moves UP to 109.5. Candle low (108.2) stays above the old
    # trailing, so no exit fires first.
    exited, _, best, nxt = process_long_minute(
        minute_open=108.5,
        minute_high=110.0,
        minute_low=108.2,
        entry_price=100.0,
        best_high=108.0,
        active_trailing=107.6,
        retrace_pct=0.05,
    )
    assert not exited
    assert best == pytest.approx(110.0)
    assert nxt == pytest.approx(109.5)
    # Equal best (no new high) must NOT move the trailing at all: candle stays
    # above the active trailing (109.5), so no gap/touch exit, and since the
    # high (109.5) does not exceed best_high, the trailing is unchanged.
    _, _, _, nxt2 = process_long_minute(
        minute_open=109.7,
        minute_high=109.5,
        minute_low=109.6,
        entry_price=100.0,
        best_high=110.0,
        active_trailing=109.5,
        retrace_pct=0.05,
    )
    assert nxt2 == pytest.approx(109.5)


def test_long_no_trailing_until_positive_profit() -> None:
    exited, _, best, nxt = process_long_minute(
        minute_open=99.0,
        minute_high=99.5,
        minute_low=98.8,
        entry_price=100.0,
        best_high=99.5,
        active_trailing=None,
        retrace_pct=0.05,
    )
    assert not exited
    assert best == pytest.approx(99.5)
    assert nxt is None


def test_short_symmetry() -> None:
    # Mirror of LONG: gap up at open exits at open*(1+slippage).
    exited, exit_price, _, nxt = process_short_minute(
        minute_open=95.5,
        minute_high=95.8,
        minute_low=95.2,
        entry_price=100.0,
        best_low=95.0,
        active_trailing=95.25,
        retrace_pct=0.05,
    )
    assert exited
    assert exit_price == pytest.approx(95.5 * (1 + 0.0001))
    assert nxt is None

    # Touch from below -> exit at trailing*(1+slippage).
    exited, exit_price, _, nxt = process_short_minute(
        minute_open=94.8,
        minute_high=95.3,
        minute_low=94.6,
        entry_price=100.0,
        best_low=95.0,
        active_trailing=95.25,
        retrace_pct=0.05,
    )
    assert exited
    assert exit_price == pytest.approx(95.25 * (1 + 0.0001))


def test_short_trailing_ratchets_down_only() -> None:
    # New low 90 -> peak 10 -> trailing = 100 - 9.5 = 90.5 (moves down).
    _, _, best, nxt = process_short_minute(
        minute_open=94.0,
        minute_high=94.5,
        minute_low=90.0,
        entry_price=100.0,
        best_low=95.0,
        active_trailing=95.25,
        retrace_pct=0.05,
    )
    assert best == pytest.approx(90.0)
    assert nxt == pytest.approx(90.5)
    # No new low (candle stays above best_low) must NOT ratchet the short
    # trailing up; candle stays below the active trailing so no exit fires.
    _, _, _, nxt2 = process_short_minute(
        minute_open=90.2,
        minute_high=90.4,
        minute_low=90.1,
        entry_price=100.0,
        best_low=90.0,
        active_trailing=90.5,
        retrace_pct=0.05,
    )
    assert nxt2 == pytest.approx(90.5)


# ------------------------------------------------------------------ wiring


def _strategy(preset: int, ctx=None) -> SupertrendXauV2RunnerStrategy:
    strategy = object.__new__(SupertrendXauV2RunnerStrategy)
    strategy.preset = preset
    strategy.retrace_pct = preset / 100.0
    strategy.factor = 3.0
    strategy.atr_period = 10
    strategy.symbol = "XAUUSDT"
    strategy.exchange = "binance"
    strategy.capital = 10_000.0
    strategy.leverage = 10.0
    strategy.position_fraction = 1.0
    strategy.fee_pct = 0.0005
    strategy.m15_warmup_bars = 320
    strategy.m1_warmup_bars = 60
    strategy.retain_bars = 320
    strategy.min_m15_bars = 80
    strategy.timestamp_semantics = "open"
    strategy.alpha_id = f"supertrend-xau-v2-{preset}pct"
    strategy.version = "1"
    strategy._last_trend_dir = 0
    strategy._last_15m_open = None
    strategy._last_1m_open = None
    strategy._pending_15m_open = None
    strategy._pending_1m_open = None
    strategy._positions = {}
    if ctx is None:
        ctx = SimpleNamespace(
            emit_signal=AsyncMock(),
            can_open_trades=lambda: True,
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


_BASE_MS = 1_786_000_000_000  # realistic ms epoch > 1e12 (not treated as seconds)


def test_v2_requests_both_15m_and_1m_channels() -> None:
    assert SupertrendXauV2RunnerStrategy.get_required_channels({"preset": 5}) == [
        "kline:15m",
        "kline:1m",
    ]
    assert SupertrendXauV2RunnerStrategy.get_required_channels({"preset": 10}) == [
        "kline:15m",
        "kline:1m",
    ]


def test_v2_warmup_tfs_include_1m() -> None:
    strategy = _strategy(5)
    assert strategy.get_warmup_tfs() == ["15m", "1m"]
    assert strategy.get_warmup_bars("15m") == 320
    assert strategy.get_warmup_bars("1m") == 60


def test_preset_validation() -> None:
    with pytest.raises(ValueError):
        SupertrendXauV2RunnerStrategy(
            alpha_id="a", version="1", params={"preset": 7}, ctx=None
        )
    strategy = _strategy(10)
    assert strategy.retrace_pct == 0.10


@pytest.mark.asyncio
async def test_1m_trailing_hit_emits_close_with_executable_ref() -> None:
    series = CandleSeries((105.2,), (105.3,), (104.7,), (104.9,), (_BASE_MS,))
    ctx = SimpleNamespace(emit_signal=AsyncMock(), cache=_cache_with(series))
    strategy = _strategy(5, ctx)
    strategy._positions = {
        "p1": {
            "position_id": "p1",
            "symbol": "XAUUSDT",
            "side": "LONG",
            "entry": 100.0,
            "qty": 1.0,
            "best_high": 105.0,
            "active_trailing": 104.75,
        }
    }
    await strategy._scan_1m(_BASE_MS)

    assert ctx.emit_signal.await_count == 1
    call = ctx.emit_signal.await_args
    assert call.args[0] == "CLOSE"
    assert call.kwargs["reason"] == "TRAILING_TP"
    assert call.kwargs["exit_price"] == pytest.approx(104.75 * (1 - 0.0001))
    import json

    assert json.loads(call.kwargs["metadata"])["ref_is_executable"] is True
    assert strategy._positions == {}


@pytest.mark.asyncio
async def test_1m_trailing_updates_state_when_not_exited() -> None:
    series = CandleSeries((106.0,), (108.0,), (105.5,), (107.0,), (_BASE_MS,))
    ctx = SimpleNamespace(emit_signal=AsyncMock(), cache=_cache_with(series))
    strategy = _strategy(5, ctx)
    strategy._positions = {
        "p1": {
            "position_id": "p1",
            "symbol": "XAUUSDT",
            "side": "LONG",
            "entry": 100.0,
            "qty": 1.0,
            "best_high": 105.0,
            "active_trailing": None,
        }
    }
    await strategy._scan_1m(_BASE_MS)

    ctx.emit_signal.assert_not_awaited()
    pos = strategy._positions["p1"]
    assert pos["best_high"] == pytest.approx(108.0)
    assert pos["active_trailing"] == pytest.approx(107.6)


@pytest.mark.asyncio
async def test_15m_flip_opens_position_and_emits_open() -> None:
    series = _rising_15m(120)
    ctx = SimpleNamespace(
        emit_signal=AsyncMock(return_value={"ok": True}),
        can_open_trades=lambda: True,
        state=SimpleNamespace(ready=True),
        price_alerts=None,
        cache=_cache_with(series),
    )
    strategy = _strategy(5, ctx)
    # 120 rising 15m bars -> uptrend; seed last_trend_dir to the OPPOSITE
    # direction and a prior candle so scan sees a real flip (not fresh start).
    series = _rising_15m(120)
    strategy._last_trend_dir = DOWNTREND
    strategy._last_15m_open = series.times[-2]
    await strategy._scan_15m(series.times[-1])

    assert ctx.emit_signal.await_count == 1
    call = ctx.emit_signal.await_args
    assert call.args[0] == "OPEN"
    assert call.kwargs["side"] == "LONG"
    assert call.kwargs["entry"] == series.closes[-1]
    expected_qty = math.floor((10_000.0 * 1.0 * 10.0 / series.closes[-1]) * 1000) / 1000
    assert call.kwargs["qty"] == pytest.approx(expected_qty)
    assert len(strategy._positions) == 1


def _rising_15m(n: int = 120, start: float = 2000.0) -> CandleSeries:
    closes = [start + i * 0.5 for i in range(n)]
    opens = [c - 0.2 for c in closes]
    highs = [c + 0.4 for c in closes]
    lows = [c - 0.4 for c in closes]
    times = [_BASE_MS + i * M15_MS for i in range(n)]
    return CandleSeries(
        tuple(opens), tuple(highs), tuple(lows), tuple(closes), tuple(times)
    )

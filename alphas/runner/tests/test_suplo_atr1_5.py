"""Correctness coverage for the suplo-atr1-5 runner strategy.

Semantics (docs/suplo_ATR1_5.py):
  - Supertrend(3,10) on RAW 15m candles drives entries (flip -> open).
  - The trailing TP (5% retrace) is simulated minute-by-minute on 1m candles,
    but only ACTIVATES once the peak profit reaches ``>= atr_multiplier *
    ATR(10)`` (the 15m ATR captured at entry). Below that gate the position
    is held with no trailing and exits only on a Supertrend flip.
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from runner.strategies.suplo_atr1_5.strategy import (
    DOWNTREND,
    M15_MS,
    CandleSeries,
    SuploAtr1RunnerStrategy,
    process_long_minute_atr1,
    process_short_minute_atr1,
)

# ------------------------------------------------------- ATR activation gate


def test_long_no_trailing_below_atr_gate() -> None:
    # Peak profit 6 < min_atr_profit 10 -> trailing must NOT activate.
    exited, exit_price, best, nxt = process_long_minute_atr1(
        minute_open=106.0,
        minute_high=108.0,
        minute_low=105.5,
        entry_price=100.0,
        best_high=102.0,
        active_trailing=None,
        min_atr_profit=10.0,
        retrace_pct=0.05,
    )
    assert not exited
    assert exit_price is None
    assert best == pytest.approx(108.0)  # peak still updated
    assert nxt is None  # but no trailing level yet


def test_long_trailing_activates_at_or_above_atr_gate() -> None:
    # Peak profit 12 >= min_atr_profit 10 -> trailing activates at 95% retrace.
    exited, exit_price, best, nxt = process_long_minute_atr1(
        minute_open=106.0,
        minute_high=112.0,
        minute_low=105.5,
        entry_price=100.0,
        best_high=102.0,
        active_trailing=None,
        min_atr_profit=10.0,
        retrace_pct=0.05,
    )
    assert not exited
    assert best == pytest.approx(112.0)
    assert nxt == pytest.approx(100.0 + 12.0 * 0.95)  # 111.4


def test_long_gap_down_at_open_exits_above_atr_gate() -> None:
    # Active trailing 104.75 (gate already met); minute opens at 104.5 (gap
    # through) -> exit at open * (1 - slippage).
    exited, exit_price, _, nxt = process_long_minute_atr1(
        minute_open=104.5,
        minute_high=105.0,
        minute_low=104.4,
        entry_price=100.0,
        best_high=105.0,
        active_trailing=104.75,
        min_atr_profit=2.0,
        retrace_pct=0.05,
    )
    assert exited
    assert exit_price == pytest.approx(104.5 * (1 - 0.0001))
    assert nxt is None


def test_long_touch_trailing_exits_at_trailing_minus_slippage() -> None:
    exited, exit_price, _, nxt = process_long_minute_atr1(
        minute_open=105.2,
        minute_high=105.3,
        minute_low=104.7,
        entry_price=100.0,
        best_high=105.0,
        active_trailing=104.75,
        min_atr_profit=2.0,
        retrace_pct=0.05,
    )
    assert exited
    assert exit_price == pytest.approx(104.75 * (1 - 0.0001))


def test_short_symmetry_below_atr_gate() -> None:
    # Mirror of LONG: peak profit too small -> no trailing, even though the
    # trailing from a previous minute would otherwise be checked.
    exited, exit_price, best, nxt = process_short_minute_atr1(
        minute_open=94.0,
        minute_high=94.5,
        minute_low=95.0,
        entry_price=100.0,
        best_low=99.0,
        active_trailing=None,
        min_atr_profit=10.0,
        retrace_pct=0.05,
    )
    assert not exited
    assert best == pytest.approx(95.0)
    assert nxt is None


def test_short_trailing_activates_above_atr_gate() -> None:
    exited, _, best, nxt = process_short_minute_atr1(
        minute_open=94.0,
        minute_high=94.5,
        minute_low=90.0,
        entry_price=100.0,
        best_low=95.0,
        active_trailing=None,
        min_atr_profit=5.0,
        retrace_pct=0.05,
    )
    assert not exited
    assert best == pytest.approx(90.0)
    assert nxt == pytest.approx(100.0 - 10.0 * 0.95)  # 90.5


def test_short_touch_exits() -> None:
    exited, exit_price, _, nxt = process_short_minute_atr1(
        minute_open=94.8,
        minute_high=95.3,
        minute_low=94.6,
        entry_price=100.0,
        best_low=95.0,
        active_trailing=95.25,
        min_atr_profit=2.0,
        retrace_pct=0.05,
    )
    assert exited
    assert exit_price == pytest.approx(95.25 * (1 + 0.0001))


# ------------------------------------------------------------------ wiring


def _strategy(ctx=None) -> SuploAtr1RunnerStrategy:
    strategy = object.__new__(SuploAtr1RunnerStrategy)
    strategy.retrace_pct = 0.05
    strategy.atr_multiplier = 1.0
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
    strategy.alpha_id = "suplo-atr1-5"
    strategy.version = "1"
    strategy._last_trend_dir = 0
    strategy._last_15m_open = None
    strategy._last_1m_open = None
    strategy._pending_15m_open = None
    strategy._pending_1m_open = None
    strategy._positions = {}
    if ctx is None:
        ctx = SimpleNamespace(
            emit_signal=AsyncMock(return_value={"ok": True}),
            can_open_trades=lambda: True,
            state=SimpleNamespace(ready=True),
            price_alerts=None,
            load_authoritative_positions=lambda: None,
            load_positions=lambda: None,
            save_positions=lambda p: None,
            clear_positions=lambda: None,
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

        def get_latest_timestamp(self, symbol, tf):
            return series.times[-1] if series.times else None

    return SimpleNamespace(
        snapshot=_Cache().snapshot, get_latest_timestamp=_Cache().get_latest_timestamp
    )


_BASE_MS = 1_786_000_000_000  # realistic ms epoch > 1e12 (not treated as seconds)


def test_requests_both_15m_and_1m_channels() -> None:
    assert SuploAtr1RunnerStrategy.get_required_channels({}) == [
        "kline:15m",
        "kline:1m",
    ]


def test_warmup_tfs_include_1m() -> None:
    strategy = _strategy()
    assert strategy.get_warmup_tfs() == ["15m", "1m"]
    assert strategy.get_warmup_bars("15m") == 320
    assert strategy.get_warmup_bars("1m") == 60


@pytest.mark.asyncio
async def test_1m_trailing_hit_emits_close_with_executable_ref() -> None:
    series = CandleSeries((105.2,), (105.3,), (104.7,), (104.9,), (_BASE_MS,))
    ctx = SimpleNamespace(
        emit_signal=AsyncMock(),
        cache=_cache_with(series),
        state=SimpleNamespace(ready=True),
    )
    strategy = _strategy(ctx)
    strategy._positions = {
        "p1": {
            "position_id": "p1",
            "symbol": "XAUUSDT",
            "side": "LONG",
            "entry": 100.0,
            "qty": 1.0,
            "best_high": 105.0,
            "active_trailing": 104.75,
            "min_atr_profit": 2.0,
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
async def test_1m_trailing_does_not_activate_below_atr_gate() -> None:
    # Peak (108) gives profit 8, but min_atr_profit 10 -> no trailing level.
    series = CandleSeries((106.0,), (108.0,), (105.5,), (107.0,), (_BASE_MS,))
    ctx = SimpleNamespace(
        emit_signal=AsyncMock(),
        cache=_cache_with(series),
        state=SimpleNamespace(ready=True),
    )
    strategy = _strategy(ctx)
    strategy._positions = {
        "p1": {
            "position_id": "p1",
            "symbol": "XAUUSDT",
            "side": "LONG",
            "entry": 100.0,
            "qty": 1.0,
            "best_high": 102.0,
            "active_trailing": None,
            "min_atr_profit": 10.0,
        }
    }
    await strategy._scan_1m(_BASE_MS)

    ctx.emit_signal.assert_not_awaited()
    pos = strategy._positions["p1"]
    assert pos["best_high"] == pytest.approx(108.0)
    assert pos["active_trailing"] is None  # gate not met -> still no trailing


@pytest.mark.asyncio
async def test_1m_trailing_activates_above_atr_gate() -> None:
    series = CandleSeries((106.0,), (112.0,), (105.5,), (111.0,), (_BASE_MS,))
    ctx = SimpleNamespace(
        emit_signal=AsyncMock(),
        cache=_cache_with(series),
        state=SimpleNamespace(ready=True),
    )
    strategy = _strategy(ctx)
    strategy._positions = {
        "p1": {
            "position_id": "p1",
            "symbol": "XAUUSDT",
            "side": "LONG",
            "entry": 100.0,
            "qty": 1.0,
            "best_high": 102.0,
            "active_trailing": None,
            "min_atr_profit": 10.0,
        }
    }
    await strategy._scan_1m(_BASE_MS)

    ctx.emit_signal.assert_not_awaited()
    pos = strategy._positions["p1"]
    assert pos["best_high"] == pytest.approx(112.0)
    assert pos["active_trailing"] == pytest.approx(100.0 + 12.0 * 0.95)


@pytest.mark.asyncio
async def test_15m_flip_opens_position_with_min_atr_profit() -> None:
    series = _rising_15m(120)
    ctx = SimpleNamespace(
        emit_signal=AsyncMock(return_value={"ok": True}),
        can_open_trades=lambda: True,
        state=SimpleNamespace(ready=True),
        price_alerts=None,
        load_authoritative_positions=lambda: None,
        load_positions=lambda: None,
        save_positions=lambda p: None,
        clear_positions=lambda: None,
        cache=_cache_with(series),
    )
    strategy = _strategy(ctx)
    strategy._last_trend_dir = DOWNTREND
    strategy._last_15m_open = series.times[-2]
    await strategy._scan_15m(series.times[-1])

    assert ctx.emit_signal.await_count == 1
    call = ctx.emit_signal.await_args
    assert call.args[0] == "OPEN"
    assert call.kwargs["side"] == "LONG"
    expected_qty = math.floor((10_000.0 * 1.0 * 10.0 / series.closes[-1]) * 1000) / 1000
    assert call.kwargs["qty"] == pytest.approx(expected_qty)
    assert len(strategy._positions) == 1
    pos = next(iter(strategy._positions.values()))
    assert pos["min_atr_profit"] > 0  # ATR-based gate captured at entry


def _rising_15m(n: int = 120, start: float = 2000.0) -> CandleSeries:
    closes = [start + i * 0.5 for i in range(n)]
    opens = [c - 0.2 for c in closes]
    highs = [c + 0.4 for c in closes]
    lows = [c - 0.4 for c in closes]
    times = [_BASE_MS + i * M15_MS for i in range(n)]
    return CandleSeries(
        tuple(opens), tuple(highs), tuple(lows), tuple(closes), tuple(times)
    )

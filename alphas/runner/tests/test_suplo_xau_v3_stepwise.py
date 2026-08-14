"""Correctness coverage for the Suplo XAU V3 Stepwise runner strategy.

Reference: docs/suplo xau v3 - stepwise.py.

Semantics:
  - Supertrend(3,10) on RAW 15m candles drives entries (flip -> open).
  - The trailing take-profit is simulated minute-by-minute on 1m candles with a
    Stepwise Dynamic r(P) retracement based on the 15m ATR profit tiers:
      P < 0.5*ATR            : no trailing (No early exit noise)
      0.5*ATR <= P < 0.75*ATR : r(P) = 35%
      0.75*ATR <= P < 1.0*ATR : r(P) = 30%
      1.0*ATR <= P < 1.5*ATR : r(P) = 25%
      P >= 1.5*ATR           : r(P) = 20%  (lock 80% peak profit)
  - Exits are triggered on 1m candle data (open/low/high), never on tick data;
    signals fire after a 1m candle closes.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from runner.strategies.suplo_xau_v3_stepwise.strategy import (
    DOWNTREND,
    UPTREND,
    CandleSeries,
    SupertrendXauV3StepwiseRunnerStrategy,
    get_stepwise_retrace_r,
    process_long_minute,
    process_short_minute,
)


# ------------------------------------------------------- Stepwise r(P) tiers


def test_r_none_below_half_atr() -> None:
    # Peak profit < 0.5 * ATR -> no retracement (no early exit noise).
    assert get_stepwise_retrace_r(peak_profit=49.0, atr_val=100.0) is None


def test_r_35pct_between_half_and_three_quarter() -> None:
    assert get_stepwise_retrace_r(peak_profit=70.0, atr_val=100.0) == pytest.approx(
        0.35
    )


def test_r_30pct_between_three_quarter_and_one() -> None:
    assert get_stepwise_retrace_r(peak_profit=90.0, atr_val=100.0) == pytest.approx(
        0.30
    )


def test_r_25pct_between_one_and_one_half() -> None:
    assert get_stepwise_retrace_r(peak_profit=140.0, atr_val=100.0) == pytest.approx(
        0.25
    )


def test_r_20pct_at_or_above_one_half() -> None:
    assert get_stepwise_retrace_r(peak_profit=160.0, atr_val=100.0) == pytest.approx(
        0.20
    )
    assert get_stepwise_retrace_r(peak_profit=100.0, atr_val=100.0) == pytest.approx(
        0.25
    )


def test_r_zero_atr_or_negative_profit_no_trailing() -> None:
    assert get_stepwise_retrace_r(peak_profit=50.0, atr_val=0.0) is None


# ------------------------------------------------------- Long minute processing


def test_long_no_trailing_below_half_atr() -> None:
    # Peak profit 40 < 0.5*ATR(100)=50 -> no trailing level.
    exited, exit_price, best, nxt = process_long_minute(
        minute_open=106.0,
        minute_high=140.0,
        minute_low=105.0,
        entry_price=100.0,
        best_high=102.0,
        active_trailing=None,
        atr_val=100.0,
    )
    assert not exited
    assert exit_price is None
    assert best == pytest.approx(140.0)  # peak still updated
    assert nxt is None  # but no trailing yet


def test_long_trailing_activates_above_half_atr() -> None:
    # Peak profit 60 (>= 0.5*ATR=50) -> tier 35% -> retain 65% of the 60 gain.
    exited, _, best, nxt = process_long_minute(
        minute_open=106.0,
        minute_high=160.0,
        minute_low=105.0,
        entry_price=100.0,
        best_high=102.0,
        active_trailing=None,
        atr_val=100.0,
    )
    assert not exited
    assert best == pytest.approx(160.0)
    assert nxt == pytest.approx(100.0 + 60.0 * 0.65)  # 139.0


def test_long_gap_down_at_open_exits() -> None:
    # Active trailing 139.0; minute opens at 138.5 (gap through) -> exit at open.
    exited, exit_price, _, nxt = process_long_minute(
        minute_open=138.5,
        minute_high=139.2,
        minute_low=138.0,
        entry_price=100.0,
        best_high=160.0,
        active_trailing=139.0,
        atr_val=100.0,
    )
    assert exited
    assert exit_price == pytest.approx(138.5 * (1 - 0.0001))
    assert nxt is None


def test_long_touch_trailing_exits() -> None:
    exited, exit_price, _, nxt = process_long_minute(
        minute_open=139.5,
        minute_high=139.8,
        minute_low=138.9,
        entry_price=100.0,
        best_high=160.0,
        active_trailing=139.0,
        atr_val=100.0,
    )
    assert exited
    assert exit_price == pytest.approx(139.0 * (1 - 0.0001))


def test_long_trailing_ratchet_never_loosens() -> None:
    # Existing trailing 135.0; new candidate trailing 139.0 -> max keeps 139.0.
    _, _, _, nxt = process_long_minute(
        minute_open=136.0,
        minute_high=160.0,
        minute_low=135.5,
        entry_price=100.0,
        best_high=160.0,
        active_trailing=135.0,
        atr_val=100.0,
    )
    assert nxt == pytest.approx(139.0)


# ------------------------------------------------------- Short minute processing


def test_short_no_trailing_below_half_atr() -> None:
    # Peak profit 40 < 0.5*ATR(100)=50 -> no trailing level.
    exited, exit_price, best, nxt = process_short_minute(
        minute_open=94.0,
        minute_high=94.5,
        minute_low=60.0,
        entry_price=100.0,
        best_low=99.0,
        active_trailing=None,
        atr_val=100.0,
    )
    assert not exited
    assert best == pytest.approx(60.0)
    assert nxt is None


def test_short_trailing_activates_above_half_atr() -> None:
    # Peak profit 55 (>= 0.5*ATR=50) -> tier 35% -> retain 65% below entry.
    exited, _, best, nxt = process_short_minute(
        minute_open=94.0,
        minute_high=94.5,
        minute_low=45.0,
        entry_price=100.0,
        best_low=95.0,
        active_trailing=None,
        atr_val=100.0,
    )
    assert not exited
    assert best == pytest.approx(45.0)
    assert nxt == pytest.approx(100.0 - 55.0 * 0.65)  # 64.25


def test_short_gap_up_at_open_exits() -> None:
    exited, exit_price, _, nxt = process_short_minute(
        minute_open=65.0,
        minute_high=65.5,
        minute_low=64.0,
        entry_price=100.0,
        best_low=45.0,
        active_trailing=64.25,
        atr_val=100.0,
    )
    assert exited
    assert exit_price == pytest.approx(65.0 * (1 + 0.0001))
    assert nxt is None


def test_short_touch_trailing_exits() -> None:
    exited, exit_price, _, nxt = process_short_minute(
        minute_open=63.5,
        minute_high=64.5,
        minute_low=63.0,
        entry_price=100.0,
        best_low=45.0,
        active_trailing=64.25,
        atr_val=100.0,
    )
    assert exited
    assert exit_price == pytest.approx(64.25 * (1 + 0.0001))


# ------------------------------------------------------------------ wiring


def _strategy(ctx=None) -> SupertrendXauV3StepwiseRunnerStrategy:
    strategy = object.__new__(SupertrendXauV3StepwiseRunnerStrategy)
    strategy.factor = 3.0
    strategy.atr_period = 10
    strategy.symbol = "PAXGUSDT"
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
    strategy.alpha_id = "suplo-xau-v3-stepwise"
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
    assert SupertrendXauV3StepwiseRunnerStrategy.get_required_channels({}) == [
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
    series = CandleSeries((138.5,), (138.8,), (138.2,), (138.6,), (_BASE_MS,))
    ctx = SimpleNamespace(
        emit_signal=AsyncMock(),
        cache=_cache_with(series),
        state=SimpleNamespace(ready=True),
    )
    strategy = _strategy(ctx)
    strategy._positions = {
        "p1": {
            "position_id": "p1",
            "symbol": "PAXGUSDT",
            "side": "LONG",
            "entry": 100.0,
            "qty": 1.0,
            "best_high": 160.0,
            "active_trailing": 139.0,
            "latest_atr": 100.0,
        }
    }
    await strategy._scan_1m(_BASE_MS)

    assert ctx.emit_signal.await_count == 1
    call = ctx.emit_signal.await_args
    assert call.args[0] == "CLOSE"
    assert call.kwargs["reason"] == "STEPWISE_TRAILING_TP_r(P)"
    assert call.kwargs["exit_price"] == pytest.approx(138.5 * (1 - 0.0001))
    assert json.loads(call.kwargs["metadata"])["ref_is_executable"] is True
    assert strategy._positions == {}


@pytest.mark.asyncio
async def test_15m_trend_flip_opens_position_at_15m_close() -> None:
    closes = [100.0 + i for i in range(90)]
    opens = [c - 0.2 for c in closes]
    highs = [c + 0.4 for c in closes]
    lows = [c - 0.4 for c in closes]
    times = [i * 15 * 60_000 for i in range(90)]
    series = CandleSeries(
        tuple(opens), tuple(highs), tuple(lows), tuple(closes), tuple(times)
    )
    ctx = SimpleNamespace(
        emit_signal=AsyncMock(),
        cache=_cache_with(series),
        state=SimpleNamespace(ready=True),
    )
    strategy = _strategy(ctx)
    await strategy._scan_15m(times[-1])

    # A fresh strategy adopts the current trend without opening on first scan.
    assert ctx.emit_signal.await_count == 0

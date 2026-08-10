"""Correctness coverage for the suplo-xau runner strategy.

The reference modules (docs/suplo-xau-5.py, docs/suplo-xau-10.py) define a pure
bar-based state machine: Supertrend(3,10) direction flip opens a position, a
dynamic trailing take-profit closes it when the price retraces ``retrace_pct``
from the peak gain, and the strategy stands in cash until the *next* flip.

This file verifies the runner port reproduces that state machine exactly and
emits the right OPEN / MODIFY / CLOSE signals.
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from runner.strategies.suplo_xau.strategy import (
    DOWNTREND,
    M15_MS,
    UPTREND,
    CandleSeries,
    SuploXauRunnerStrategy,
    compute_supertrend_directions,
    step_state_machine,
)


# ---------------------------------------------------------------- reference port
# Independent, verbatim port of the reference docs' generate_alpha_signals_*
# loop, used only to cross-check the runner's step_state_machine on synthetic
# series (the strongest parity evidence is checking many candles in one run).


def _reference_state_machine(
    closes, highs, lows, directions, retrace_pct
) -> list[dict[str, object]]:
    n = len(closes)
    current_pos = 0
    last_trend_dir = 0
    entry_p = 0.0
    peak_p = 0.0
    out: list[dict[str, object]] = []
    for i in range(1, n):
        d_curr = directions[i]
        target_exit = None
        trailing_exit = False
        if current_pos != 0:
            high_i, low_i = highs[i], lows[i]
            curr_gain = (high_i - entry_p) if current_pos == 1 else (entry_p - low_i)
            if curr_gain > peak_p:
                peak_p = curr_gain
            if peak_p > 0:
                allowed_gain = peak_p * (1.0 - retrace_pct)
                target_exit = (
                    entry_p + allowed_gain
                    if current_pos == 1
                    else entry_p - allowed_gain
                )
                hit_exit = (
                    low_i <= target_exit if current_pos == 1 else high_i >= target_exit
                )
                if hit_exit:
                    current_pos = 0
                    peak_p = 0.0
                    trailing_exit = True
        flip_exit = False
        flip_open = False
        side = None
        if d_curr != last_trend_dir:
            last_trend_dir = d_curr
            if d_curr != 0:
                if current_pos != 0:
                    flip_exit = True
                current_pos = -1 if d_curr == 1 else 1
                entry_p = closes[i]
                peak_p = 0.0
                flip_open = True
                side = "SHORT" if current_pos == -1 else "LONG"
        out.append(
            {
                "position": current_pos,
                "entry": entry_p,
                "peak": peak_p,
                "target": target_exit,
                "trailing_exit": trailing_exit,
                "flip_exit": flip_exit,
                "flip_open": flip_open,
                "side": side,
            }
        )
    return out


# ---------------------------------------------------------------- fixture helpers


def _trend_series(
    n: int = 200,
    *,
    slope: float = 0.0,
    start: float = 2000.0,
    wick: float = 0.4,
    body: float = 0.2,
) -> CandleSeries:
    """Monotone (or flat) price path with a small body/wick for stable supertrend."""
    closes = [start + slope * i for i in range(n)]
    opens = [c - body for c in closes]
    highs = [c + wick for c in closes]
    lows = [c - wick for c in closes]
    times = [i * M15_MS for i in range(n)]
    return CandleSeries(
        tuple(opens), tuple(highs), tuple(lows), tuple(closes), tuple(times)
    )


def _up_then_down(
    n: int = 240, up_bars: int = 120, start: float = 2000.0
) -> CandleSeries:
    """Rising then falling path with a visible peak -- exercises the trailing TP."""
    closes = [start + i * 0.5 for i in range(up_bars)]
    closes += [closes[-1] - (i - up_bars) * 0.5 for i in range(up_bars, n)]
    opens = [c - 0.2 for c in closes]
    highs = [c + 0.4 for c in closes]
    lows = [c - 0.4 for c in closes]
    times = [i * M15_MS for i in range(n)]
    return CandleSeries(
        tuple(opens), tuple(highs), tuple(lows), tuple(closes), tuple(times)
    )


def _strategy(preset: int, ctx=None) -> SuploXauRunnerStrategy:
    strategy = object.__new__(SuploXauRunnerStrategy)
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
    strategy.alpha_id = f"suplo-xau-{preset}"
    strategy.version = "1"
    strategy._last_trend_dir = 0
    strategy._last_15m_open = None
    strategy._pending_15m_open = None
    strategy._positions = {}
    strategy.m15_warmup_bars = 320
    strategy.retain_bars = 320
    strategy.min_bars = 80
    strategy.timestamp_semantics = "open"
    strategy.ctx = (
        ctx
        if ctx is not None
        else SimpleNamespace(
            emit_signal=AsyncMock(),
            can_open_trades=lambda: True,
            state=SimpleNamespace(ready=True),
            price_alerts=None,
            cache=SimpleNamespace(),
        )
    )
    return strategy


# ------------------------------------------------------------- state machine parity


def test_state_machine_matches_reference_on_uptrend() -> None:
    series = _trend_series(n=220, slope=0.5)
    directions = compute_supertrend_directions(series.highs, series.lows, series.closes)
    reference = _reference_state_machine(
        list(series.closes), list(series.highs), list(series.lows), directions, 0.05
    )
    current_pos, entry_p, peak_p, last_trend_dir = 0, 0.0, 0.0, 0
    for i in range(1, len(series.closes)):
        step = step_state_machine(
            current_pos=current_pos,
            entry_p=entry_p,
            peak_p=peak_p,
            last_trend_dir=last_trend_dir,
            high=series.highs[i],
            low=series.lows[i],
            close=series.closes[i],
            direction=directions[i],
            retrace_pct=0.05,
        )
        ref = reference[i - 1]
        assert step.position == ref["position"], f"candle {i}: pos"
        assert step.entry_price == pytest.approx(ref["entry"], abs=1e-9), (
            f"candle {i}: entry"
        )
        assert step.peak_gain == pytest.approx(ref["peak"], abs=1e-9), (
            f"candle {i}: peak"
        )
        assert step.trailing_exit_hit == ref["trailing_exit"], f"candle {i}: trailing"
        assert step.flip_exit_hit == ref["flip_exit"], f"candle {i}: flip exit"
        assert step.flip_open == ref["flip_open"], f"candle {i}: flip open"
        assert step.side == ref["side"], f"candle {i}: side"
        if ref["target"] is None:
            assert step.target_exit_price is None, f"candle {i}: target"
        else:
            assert step.target_exit_price == pytest.approx(ref["target"], abs=1e-9), (
                f"candle {i}: target"
            )
        current_pos, entry_p, peak_p, last_trend_dir = (
            step.position,
            step.entry_price,
            step.peak_gain,
            step.last_trend_dir,
        )
    # Parity is the contract: the runner port must reproduce the reference
    # loop exactly on every candle.  The final state itself is data-dependent
    # (a fixed wick eventually pierces the 5%-of-gain trailing gap), so assert
    # equality with the reference rather than a hardcoded position.
    last_step = step_state_machine(
        current_pos=current_pos,
        entry_p=entry_p,
        peak_p=peak_p,
        last_trend_dir=last_trend_dir,
        high=series.highs[-1],
        low=series.lows[-1],
        close=series.closes[-1],
        direction=directions[-1],
        retrace_pct=0.05,
    )
    assert last_step.position == reference[-1]["position"]


def test_state_machine_matches_reference_on_up_down() -> None:
    series = _up_then_down()
    directions = compute_supertrend_directions(series.highs, series.lows, series.closes)
    for retrace in (0.05, 0.10):
        reference = _reference_state_machine(
            list(series.closes),
            list(series.highs),
            list(series.lows),
            directions,
            retrace,
        )
        current_pos, entry_p, peak_p, last_trend_dir = 0, 0.0, 0.0, 0
        for i in range(1, len(series.closes)):
            step = step_state_machine(
                current_pos=current_pos,
                entry_p=entry_p,
                peak_p=peak_p,
                last_trend_dir=last_trend_dir,
                high=series.highs[i],
                low=series.lows[i],
                close=series.closes[i],
                direction=directions[i],
                retrace_pct=retrace,
            )
            ref = reference[i - 1]
            assert step.position == ref["position"], f"{retrace} candle {i}: pos"
            assert step.peak_gain == pytest.approx(ref["peak"], abs=1e-9), (
                f"{retrace} candle {i}: peak"
            )
            assert step.trailing_exit_hit == ref["trailing_exit"], (
                f"{retrace} candle {i}: trailing"
            )
            assert step.flip_open == ref["flip_open"], f"{retrace} candle {i}: open"
            assert step.side == ref["side"], f"{retrace} candle {i}: side"
            current_pos, entry_p, peak_p, last_trend_dir = (
                step.position,
                step.entry_price,
                step.peak_gain,
                step.last_trend_dir,
            )
        assert reference[-1]["position"] == 0  # fell back out of the trailing zone


def test_direction_constants_map_to_sides() -> None:
    # A flip to DOWNTREND (1) opens SHORT; a flip to UPTREND (-1) opens LONG.
    step_short = step_state_machine(
        current_pos=0,
        entry_p=0.0,
        peak_p=0.0,
        last_trend_dir=UPTREND,
        high=10,
        low=9,
        close=9.5,
        direction=DOWNTREND,
        retrace_pct=0.05,
    )
    assert (
        step_short.flip_open
        and step_short.side == "SHORT"
        and step_short.position == -1
    )
    step_long = step_state_machine(
        current_pos=0,
        entry_p=0.0,
        peak_p=0.0,
        last_trend_dir=DOWNTREND,
        high=10,
        low=9,
        close=9.5,
        direction=UPTREND,
        retrace_pct=0.05,
    )
    assert step_long.flip_open and step_long.side == "LONG" and step_long.position == 1


def test_trailing_exit_triggers_at_retrace_from_peak() -> None:
    # LONG at 100. Candle high reaches 110 (peak gain 10). A 5% retrace allows
    # 9.5 -> target 109.5. A low of 109 pierces it -> trailing exit.
    step = step_state_machine(
        current_pos=1,
        entry_p=100.0,
        peak_p=0.0,
        last_trend_dir=UPTREND,
        high=110.0,
        low=109.0,
        close=109.2,
        direction=UPTREND,
        retrace_pct=0.05,
    )
    assert step.trailing_exit_hit
    assert step.position == 0
    assert step.target_exit_price == pytest.approx(109.5)


def test_stand_in_cash_until_next_flip_after_trailing_exit() -> None:
    # After a trailing exit, the SAME trend direction must NOT reopen a position.
    step = step_state_machine(
        current_pos=0,
        entry_p=0.0,
        peak_p=0.0,
        last_trend_dir=UPTREND,
        high=111,
        low=109.2,
        close=110,
        direction=UPTREND,
        retrace_pct=0.05,
    )
    assert step.position == 0
    assert not step.flip_open
    assert not step.flip_exit_hit


def test_flip_while_in_position_closes_old_and_opens_new() -> None:
    # LONG open at 100 with peak 5.0 (trailing target = 100 + 5*0.95 = 104.75).
    # This candle's low (104.9) stays above the target, so the trailing check
    # must NOT fire; the direction flip closes the LONG and opens a SHORT at the
    # candle close (104.9).
    step = step_state_machine(
        current_pos=1,
        entry_p=100.0,
        peak_p=5.0,
        last_trend_dir=UPTREND,
        high=105.0,
        low=104.9,
        close=104.9,
        direction=DOWNTREND,
        retrace_pct=0.05,
    )
    assert step.flip_exit_hit
    assert step.flip_open
    assert step.side == "SHORT"
    assert step.position == -1
    assert step.entry_price == pytest.approx(104.9)
    assert step.trailing_exit_hit is False


def test_trailing_check_has_priority_over_flip_on_same_candle() -> None:
    # Reference ordering: the trailing check runs FIRST. If the flip candle's
    # low pierces the trailing target, the old position exits via TRAILING_TP,
    # and the flip still opens the new opposite position. flip_exit_hit stays
    # False because the position was already closed by the trailing check.
    step = step_state_machine(
        current_pos=1,
        entry_p=100.0,
        peak_p=5.0,
        last_trend_dir=UPTREND,
        high=107.0,
        low=104.0,
        close=104.5,
        direction=DOWNTREND,
        retrace_pct=0.05,
    )
    # curr_gain = 7 -> peak = 7 -> target = 100 + 7*0.95 = 106.65; low 104 pierces it.
    assert step.trailing_exit_hit
    assert step.flip_exit_hit is False
    assert step.flip_open
    assert step.side == "SHORT"
    assert step.position == -1


# ------------------------------------------------------------------- runner wiring


class _Cache:
    def snapshot(self, symbol: str, tf: str, bars: int):
        assert symbol == "XAUUSDT"
        assert tf == "15m"
        # Unix SECONDS (0, 15m, 30m, 45m) -- normalized to ms by _timestamp_ms.
        return SimpleNamespace(
            times=(0, 900, 1800, 2700),
            opens=(100.0, 102.0, 104.0, 105.0),
            highs=(103.0, 105.0, 106.0, 107.0),
            lows=(99.0, 101.0, 103.0, 104.0),
            closes=(102.0, 104.0, 105.0, 106.0),
        )


def test_series_uses_raw_15m_candles() -> None:
    # The reference module operates on a plain OHLC dataframe; the V1 family
    # runs on raw 15m candles (no M30 aggregation). Timestamps are normalized
    # from seconds to milliseconds.
    strategy = object.__new__(SuploXauRunnerStrategy)
    strategy.symbol = "XAUUSDT"
    strategy.ctx = SimpleNamespace(cache=_Cache())
    strategy.timestamp_semantics = "open"
    strategy.get_retain_bars = lambda tf: 10

    series = strategy._series()

    assert series.times == (0, 900000, 1800000, 2700000)
    assert series.opens == (100.0, 102.0, 104.0, 105.0)
    assert series.highs == (103.0, 105.0, 106.0, 107.0)
    assert series.lows == (99.0, 101.0, 103.0, 104.0)
    assert series.closes == (102.0, 104.0, 105.0, 106.0)


def test_suplo_strategy_requests_only_m15_channel() -> None:
    assert SuploXauRunnerStrategy.get_required_channels({"preset": 5}) == ["kline:15m"]
    assert SuploXauRunnerStrategy.get_required_channels({"preset": 10}) == ["kline:15m"]


def test_preset_validation() -> None:
    strategy = _strategy(10)
    assert strategy.retrace_pct == 0.10
    assert strategy.retrace_pct == 10 / 100.0
    # get_required_channels is a classmethod and does not validate preset.
    assert SuploXauRunnerStrategy.get_required_channels({"preset": 7}) == ["kline:15m"]
    with pytest.raises(ValueError):
        SuploXauRunnerStrategy(
            alpha_id="a", version="1", params={"preset": 7}, ctx=None
        )


@pytest.mark.asyncio
async def test_open_if_allowed_emits_open_and_sizes_qty() -> None:
    ctx = SimpleNamespace(
        emit_signal=AsyncMock(return_value={"ok": True}),
        can_open_trades=lambda: True,
        state=SimpleNamespace(ready=True),
        price_alerts=None,
        cache=SimpleNamespace(),
    )
    strategy = _strategy(5, ctx)
    entry = 2000.0
    await strategy._open_if_allowed("LONG", entry, 123456)

    assert ctx.emit_signal.await_count == 1
    call = ctx.emit_signal.await_args
    assert call.args[0] == "OPEN"
    assert call.kwargs["side"] == "LONG"
    assert call.kwargs["entry"] == entry
    expected_qty = math.floor((10_000.0 * 1.0 * 10.0 / entry) * 1000) / 1000
    assert call.kwargs["qty"] == pytest.approx(expected_qty)
    assert len(strategy._positions) == 1


@pytest.mark.asyncio
async def test_open_if_allowed_skips_when_position_open() -> None:
    ctx = SimpleNamespace(emit_signal=AsyncMock())
    strategy = _strategy(5, ctx)
    strategy._positions = {
        "p1": {
            "position_id": "p1",
            "symbol": "XAUUSDT",
            "side": "LONG",
            "entry": 2000.0,
            "qty": 0.05,
        }
    }
    await strategy._open_if_allowed("SHORT", 2000.0, 123456)
    ctx.emit_signal.assert_not_awaited()
    assert len(strategy._positions) == 1


@pytest.mark.asyncio
async def test_manage_on_bar_closes_on_trailing_tp() -> None:
    ctx = SimpleNamespace(emit_signal=AsyncMock())
    strategy = _strategy(5, ctx)
    strategy._positions = {
        "p1": {
            "position_id": "p1",
            "symbol": "XAUUSDT",
            "side": "LONG",
            "entry": 100.0,
            "qty": 1.0,
            "peak_gain": 0.0,
        }
    }
    series = CandleSeries((100.0,), (110.0,), (109.0,), (109.2,), (123456,))
    await strategy._manage_on_bar(series)

    assert ctx.emit_signal.await_count == 1
    call = ctx.emit_signal.await_args
    assert call.args[0] == "CLOSE"
    assert call.kwargs["reason"] == "TRAILING_TP"
    assert call.kwargs["exit_price"] == pytest.approx(109.5)
    assert strategy._positions == {}


@pytest.mark.asyncio
async def test_manage_on_bar_emits_modify_when_trailing_level_rises() -> None:
    ctx = SimpleNamespace(emit_signal=AsyncMock())
    strategy = _strategy(5, ctx)
    strategy._positions = {
        "p1": {
            "position_id": "p1",
            "symbol": "XAUUSDT",
            "side": "LONG",
            "entry": 100.0,
            "qty": 1.0,
            "peak_gain": 0.0,
        }
    }
    series = CandleSeries((100.0,), (108.0,), (107.7,), (107.8,), (123456,))
    await strategy._manage_on_bar(series)

    assert ctx.emit_signal.await_count == 1
    call = ctx.emit_signal.await_args
    assert call.args[0] == "MODIFY"
    # peak gain = 8 -> allowed = 8 * 0.95 = 7.6 -> target = 107.6; the candle
    # low (107.7) stays above the target, so no trailing exit, just MODIFY.
    assert call.kwargs["tp"] == pytest.approx(107.6)
    assert strategy._positions["p1"]["peak_gain"] == pytest.approx(8.0)

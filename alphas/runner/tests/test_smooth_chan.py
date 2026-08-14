"""Correctness coverage for the smooth_chan runner strategy.

Reference: docs/Downloads/alpha/backtest_smooth_chan.py + exit_lab.compute_supertrend_multi.

Semantics:
  - Supertrend(factor, 10) on closed effective candles (1h, or 3h built from 1h)
    drives entries: a flip to downtrend (1) opens SHORT, a flip to uptrend (-1)
    opens LONG.
  - A smooth chandelier stop trails the watermark; the chan multiplier drips from
    chan_start down to chan_min as peak profit approaches scale_pct.
  - The chandelier uses the ENTRY ATR (fixed at trade open).
  - Optional breakeven (be_pct) and flip-only modes.
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from runner.strategies.smooth_chan.strategy import (
    DOWNTREND,
    H1_MS,
    H3_MS,
    M15_MS,
    UPTREND,
    CandleSeries,
    SmoothChanRunnerStrategy,
    compute_supertrend_multi,
)

_BASE_MS = 1_786_000_000_000  # realistic ms epoch > 1e12 (not treated as seconds)
# A 3h-aligned base so H3 bucket starts land on multiples of H3_MS.
_H3_BASE = _BASE_MS - (_BASE_MS % H3_MS)
_N_BARS = 120  # well above min_bars=80 for 1h


def _series(
    OHLC: list[tuple[float, float, float, float]], tf_ms: int = H1_MS
) -> CandleSeries:
    opens = [_o for _o, _h, _l, _c in OHLC]
    highs = [_h for _o, _h, _l, _c in OHLC]
    lows = [_l for _o, _h, _l, _c in OHLC]
    closes = [_c for _o, _h, _l, _c in OHLC]
    times = [_BASE_MS + i * tf_ms for i in range(len(OHLC))]
    return CandleSeries(
        tuple(opens), tuple(highs), tuple(lows), tuple(closes), tuple(times)
    )


def _rising(n: int = _N_BARS) -> list[tuple[float, float, float, float]]:
    return [
        (
            2000 + i * 0.5 - 0.2,
            2000 + i * 0.5 + 0.4,
            2000 + i * 0.5 - 0.4,
            2000 + i * 0.5,
        )
        for i in range(n)
    ]


def _falling(n: int = _N_BARS) -> list[tuple[float, float, float, float]]:
    return [
        (
            2000 - i * 0.5 - 0.2,
            2000 - i * 0.5 + 0.4,
            2000 - i * 0.5 - 0.4,
            2000 - i * 0.5,
        )
        for i in range(n)
    ]


# ------------------------------------------------------------- indicator math


def test_supertrend_rising_is_uptrend() -> None:
    s = _series(_rising())
    direction, _ = compute_supertrend_multi(s.highs, s.lows, s.closes, 2.5, 10)
    assert direction[-1] == UPTREND


def test_supertrend_falling_is_downtrend() -> None:
    s = _series(_falling())
    direction, _ = compute_supertrend_multi(s.highs, s.lows, s.closes, 2.5, 10)
    assert direction[-1] == DOWNTREND


def test_atr_tail_is_finite() -> None:
    s = _series(_rising(80))
    _, atr = compute_supertrend_multi(s.highs, s.lows, s.closes, 2.5, 10)
    assert all(math.isfinite(v) for v in atr[-5:])


# ------------------------------------------------------------------ wiring


def _strategy(ctx=None, tf: str = "1h", **overrides) -> SmoothChanRunnerStrategy:
    strategy = object.__new__(SmoothChanRunnerStrategy)
    strategy.tf = tf
    strategy.source_tf = "15m" if tf == "15m" else "1h"
    strategy.source_ms = 15 * 60 * 1000 if tf == "15m" else H1_MS
    strategy.factor = overrides.get("factor", 2.5)
    strategy.atr_period = overrides.get("atr_period", 10)
    strategy.chan_start = overrides.get("chan_start", 3.0)
    strategy.chan_min = overrides.get("chan_min", 2.0)
    strategy.scale_pct = overrides.get("scale_pct", 0.15)
    strategy.be_pct = overrides.get("be_pct", 0.02)
    strategy.sl_k = overrides.get("sl_k", 0.0)
    strategy.flip_only = overrides.get("flip_only", False)
    strategy.symbol = "PAXGUSDT"
    strategy.exchange = "binance"
    strategy.capital = 10_000.0
    strategy.leverage = 10.0
    strategy.position_fraction = 1.0
    strategy.fee_pct = 0.0005
    strategy.warmup_bars = 1440 if tf == "15m" else 320 if tf == "1h" else 960
    strategy.retain_bars = overrides.get("retain_bars", strategy.warmup_bars)
    strategy.min_bars = 80 if tf == "1h" else 30
    strategy.timestamp_semantics = "open"
    strategy.alpha_id = "smooth-chan-test"
    strategy.version = "1"
    strategy._last_dir = 0
    strategy._last_eff_open = None
    strategy._pending_eff_open = None
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


def test_requests_1h_channel() -> None:
    assert SmoothChanRunnerStrategy.get_required_channels({}) == ["kline:1h"]


def test_requests_15m_channel_when_tf_15m() -> None:
    assert SmoothChanRunnerStrategy.get_required_channels({"tf": "15m"}) == [
        "kline:15m"
    ]


def test_15m_warmup_tfs_and_bars() -> None:
    strategy = _strategy(tf="15m")
    assert strategy.get_warmup_symbols() == ["PAXGUSDT"]
    assert strategy.get_warmup_tfs() == ["15m"]
    assert strategy.get_warmup_bars("15m") == 1440


def test_warmup_tfs_and_bars() -> None:
    strategy = _strategy()
    assert strategy.get_warmup_symbols() == ["PAXGUSDT"]
    assert strategy.get_warmup_tfs() == ["1h"]
    assert strategy.get_warmup_bars("1h") == 320


def test_registry_registers_strategy() -> None:
    from runner.strategy.registry import StrategyRegistry

    from runner.strategies.smooth_chan import register

    registry = StrategyRegistry()
    register(registry)
    assert "smooth_chan" in registry.names()
    assert registry.get_class("smooth_chan") is SmoothChanRunnerStrategy


# ------------------------------------------------------------- H3 aggregation


def test_h3_series_builds_completed_3h_bars() -> None:
    OHLC: list[tuple[float, float, float, float]] = []
    for bucket in range(3):
        for hour in range(3):
            base = 2000 + bucket * 10
            OHLC.append(
                (base + hour, base + hour + 2, base + hour - 2, base + hour + 1)
            )

    class _AlignedCache:
        def snapshot(self, symbol, tf, bars):
            # 9 1h bars across 3 complete 3h buckets, aligned to 3h boundaries.
            times = tuple(
                _H3_BASE + b * H3_MS + h * H1_MS for b in range(3) for h in range(3)
            )
            return SimpleNamespace(
                opens=tuple(o for o, _h, _l, _c in OHLC),
                highs=tuple(h for _o, h, _l, _c in OHLC),
                lows=tuple(l for _o, _h, l, _c in OHLC),
                closes=tuple(c for _o, _h, _l, c in OHLC),
                times=times,
            )

    strategy = _strategy(tf="3h")
    strategy.ctx = SimpleNamespace(
        emit_signal=AsyncMock(),
        load_authoritative_positions=lambda: None,
        load_positions=lambda: None,
        state=SimpleNamespace(ready=True),
        price_alerts=None,
        cache=SimpleNamespace(snapshot=_AlignedCache().snapshot),
    )
    eff = strategy._eff_series(100)
    assert len(eff.closes) == 3  # 3 complete 3h buckets
    assert eff.closes[0] == pytest.approx(OHLC[2][3])
    assert eff.closes[1] == pytest.approx(OHLC[5][3])
    assert eff.closes[2] == pytest.approx(OHLC[8][3])


def test_should_scan_3h_only_on_bucket_completion() -> None:
    strategy = _strategy(tf="3h")
    strategy.ctx.cache = SimpleNamespace(
        get_latest_timestamp=lambda symbol, tf: _H3_BASE + 2 * H1_MS
    )
    # 1h bar at +2h completes a 3h bucket (start % 3h == 2h).
    assert strategy.should_scan_after_event("kline", "PAXGUSDT", "1h") is True
    # A 1h bar at +1h does not complete a bucket.
    strategy.ctx.cache.get_latest_timestamp = lambda symbol, tf: _H3_BASE + 1 * H1_MS
    assert strategy.should_scan_after_event("kline", "PAXGUSDT", "1h") is False


# ------------------------------------------------------------- entry on flip


@pytest.mark.asyncio
async def test_1h_flip_to_uptrend_opens_long() -> None:
    series = _series(_rising())
    ctx = SimpleNamespace(
        emit_signal=AsyncMock(return_value={"ok": True}),
        load_authoritative_positions=lambda: None,
        load_positions=lambda: None,
        state=SimpleNamespace(ready=True),
        price_alerts=None,
        cache=_cache_with(series),
    )
    strategy = _strategy(ctx)
    strategy._last_dir = DOWNTREND
    strategy._last_eff_open = series.times[-1] - H1_MS
    await strategy._scan_eff(series.times[-1])

    assert ctx.emit_signal.await_count == 1
    call = ctx.emit_signal.await_args
    assert call.args[0] == "OPEN"
    assert call.kwargs["side"] == "LONG"
    assert call.kwargs["symbol"] == "PAXGUSDT"
    assert len(strategy._positions) == 1


@pytest.mark.asyncio
async def test_1h_flip_to_downtrend_opens_short() -> None:
    series = _series(_falling())
    ctx = SimpleNamespace(
        emit_signal=AsyncMock(return_value={"ok": True}),
        load_authoritative_positions=lambda: None,
        load_positions=lambda: None,
        state=SimpleNamespace(ready=True),
        price_alerts=None,
        cache=_cache_with(series),
    )
    strategy = _strategy(ctx)
    strategy._last_dir = UPTREND
    strategy._last_eff_open = series.times[-1] - H1_MS
    await strategy._scan_eff(series.times[-1])

    assert ctx.emit_signal.await_count == 1
    call = ctx.emit_signal.await_args
    assert call.args[0] == "OPEN"
    assert call.kwargs["side"] == "SHORT"


@pytest.mark.asyncio
async def test_15m_flip_to_uptrend_opens_long() -> None:
    series = _series(_rising(), tf_ms=M15_MS)
    ctx = SimpleNamespace(
        emit_signal=AsyncMock(return_value={"ok": True}),
        load_authoritative_positions=lambda: None,
        load_positions=lambda: None,
        state=SimpleNamespace(ready=True),
        price_alerts=None,
        cache=_cache_with(series),
    )
    strategy = _strategy(ctx, tf="15m")
    strategy._last_dir = DOWNTREND
    strategy._last_eff_open = series.times[-1] - M15_MS
    await strategy._scan_eff(series.times[-1])

    assert ctx.emit_signal.await_count == 1
    call = ctx.emit_signal.await_args
    assert call.args[0] == "OPEN"
    assert call.kwargs["side"] == "LONG"
    assert call.kwargs["symbol"] == "PAXGUSDT"
    assert call.kwargs["tf"] == "15m"
    assert len(strategy._positions) == 1


# ------------------------------------------------------- trend flip closes pos


@pytest.mark.asyncio
async def test_trend_flip_closes_opposite_position() -> None:
    series = _series(_rising())
    ctx = SimpleNamespace(
        emit_signal=AsyncMock(return_value={"ok": True}),
        load_authoritative_positions=lambda: None,
        load_positions=lambda: None,
        state=SimpleNamespace(ready=True),
        price_alerts=None,
        cache=_cache_with(series),
    )
    strategy = _strategy(ctx)
    strategy._last_dir = DOWNTREND
    strategy._last_eff_open = series.times[-1] - H1_MS
    strategy._positions = {
        "p1": {
            "position_id": "p1",
            "symbol": "PAXGUSDT",
            "side": "SHORT",
            "entry": 2600.0,
            "qty": 1.0,
            "entry_atr": 10.0,
            "wm": 2600.0,
            "be": False,
        }
    }
    await strategy._scan_eff(series.times[-1])

    reasons = [c.args[0] for c in ctx.emit_signal.await_args_list]
    assert reasons == ["CLOSE", "OPEN"]
    assert ctx.emit_signal.await_args_list[0].kwargs["reason"] == "TREND_FLIP"
    assert ctx.emit_signal.await_args_list[1].kwargs["side"] == "LONG"
    assert len(strategy._positions) == 1


# --------------------------------------------------------- smooth chan / BE exit


def _position(
    side: str, entry: float, entry_atr: float, wm: float, be: bool = False
) -> dict:
    return {
        "position_id": "p1",
        "symbol": "PAXGUSDT",
        "side": side,
        "entry": entry,
        "qty": 1.0,
        "entry_atr": entry_atr,
        "wm": wm,
        "be": be,
    }


@pytest.mark.asyncio
async def test_chandelier_exit_when_low_breaks_chan_stop(monkeypatch) -> None:
    import runner.strategies.smooth_chan.strategy as mod

    # A LONG whose last CLOSED bar's low drops below the (tightened) chan stop.
    # wm = 2050 (peak high), entry = 2000, entry_atr = 10, profit ~2.5%.
    # scale 0.15 -> chan mult ~2.83 -> stop ~2021.7 > entry. The last closed bar
    # low (1900) breaks it. The final bar is the new-open execution reference.
    OHLC = _rising() + [
        (2600.0, 2601.0, 1900.0, 2000.0),  # last CLOSED bar: low breaks stop
        (2600.0, 2601.0, 2600.0, 2600.0),  # new-open execution bar (not evaluated)
    ]
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
    strategy._last_dir = UPTREND
    strategy._last_eff_open = series.times[-1] - H1_MS
    strategy._positions = {"p1": _position("LONG", 2000.0, 10.0, 2050.0)}
    # Force supertrend to stay UPTREND so no flip exit interferes.
    monkeypatch.setattr(
        mod,
        "compute_supertrend_multi",
        lambda highs, lows, closes, factor, atr_period: (
            [UPTREND] * len(closes),
            [10.0] * len(closes),
        ),
    )
    await strategy._scan_eff(series.times[-1])

    assert ctx.emit_signal.await_count == 1
    call = ctx.emit_signal.await_args
    assert call.args[0] == "CLOSE"
    assert call.kwargs["reason"] == "CH"
    assert strategy._positions == {}


@pytest.mark.asyncio
async def test_no_exit_when_chan_stop_below_entry(monkeypatch) -> None:
    import runner.strategies.smooth_chan.strategy as mod

    # profit_pct = 0 -> chan_mult = chan_start = 3.0 -> stop = wm - 3*atr.
    # With wm close to entry the stop stays below entry -> no chandelier exit.
    # Closed bar high (2000) does not raise wm (2000), so profit stays 0.
    OHLC = _rising() + [
        (1990.0, 2000.0, 990.0, 1990.0),  # last CLOSED bar: low 990, high == wm
        (2600.0, 2601.0, 2600.0, 2600.0),  # new-open execution bar
    ]
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
    strategy._last_dir = UPTREND
    strategy._last_eff_open = series.times[-1] - H1_MS
    strategy._positions = {"p1": _position("LONG", 2000.0, 10.0, 2000.0)}
    monkeypatch.setattr(
        mod,
        "compute_supertrend_multi",
        lambda highs, lows, closes, factor, atr_period: (
            [UPTREND] * len(closes),
            [10.0] * len(closes),
        ),
    )
    await strategy._scan_eff(series.times[-1])

    assert ctx.emit_signal.await_count == 0
    assert len(strategy._positions) == 1


@pytest.mark.asyncio
async def test_breakeven_exit_after_be_armed(monkeypatch) -> None:
    import runner.strategies.smooth_chan.strategy as mod

    # LONG armed BE (high > entry*1.02) then the last CLOSED bar's low dips to
    # entry -> BE exit. wm stays at entry (closed bar high doesn't exceed it) so
    # the chandelier stop stays at wm - chan_start*atr = 2000 - 30 = 1970 < entry,
    # i.e. CH cannot fire before BE.
    OHLC = _rising() + [
        (2000.0, 2000.0, 2000.0, 2000.0),  # last CLOSED bar: low == entry
        (2600.0, 2601.0, 2600.0, 2600.0),  # new-open execution bar
    ]
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
    strategy._last_dir = UPTREND
    strategy._last_eff_open = series.times[-1] - H1_MS
    strategy._positions = {"p1": _position("LONG", 2000.0, 10.0, 2000.0, be=True)}
    monkeypatch.setattr(
        mod,
        "compute_supertrend_multi",
        lambda highs, lows, closes, factor, atr_period: (
            [UPTREND] * len(closes),
            [10.0] * len(closes),
        ),
    )
    await strategy._scan_eff(series.times[-1])

    assert ctx.emit_signal.await_count == 1
    call = ctx.emit_signal.await_args
    assert call.args[0] == "CLOSE"
    assert call.kwargs["reason"] == "BE"
    assert strategy._positions == {}


@pytest.mark.asyncio
async def test_flip_only_mode_never_uses_chan_or_be(monkeypatch) -> None:
    import runner.strategies.smooth_chan.strategy as mod

    strategy = _strategy(flip_only=True)
    OHLC = _rising() + [
        (2600.0, 2601.0, 1900.0, 2000.0),
        (2600.0, 2601.0, 2600.0, 2600.0),
    ]
    series = _series(OHLC)
    ctx = SimpleNamespace(
        emit_signal=AsyncMock(return_value={"ok": True}),
        load_authoritative_positions=lambda: None,
        load_positions=lambda: None,
        state=SimpleNamespace(ready=True),
        price_alerts=None,
        cache=_cache_with(series),
    )
    strategy.ctx = ctx
    strategy._last_dir = UPTREND
    strategy._last_eff_open = series.times[-1] - H1_MS
    strategy._positions = {"p1": _position("LONG", 2000.0, 10.0, 2060.0, be=True)}
    monkeypatch.setattr(
        mod,
        "compute_supertrend_multi",
        lambda highs, lows, closes, factor, atr_period: (
            [UPTREND] * len(closes),
            [10.0] * len(closes),
        ),
    )
    await strategy._scan_eff(series.times[-1])

    assert ctx.emit_signal.await_count == 0
    assert len(strategy._positions) == 1

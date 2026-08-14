"""Correctness coverage for the XAU V3 MFE ATR Distance runner strategy.

Reference: docs/xau v3 - mfe ATR.py.

Semantics:
  - Supertrend(3,10) on RAW 15m candles drives entries (flip -> open).
  - The trailing TP uses MFE ATR Distance based on the 15m Snapshot ATR(10):
      MFE < 0.5 * ATR                     : no trailing
      MFE >= 0.5 * ATR AND age >= 2 min   : Distance = 0.35 * ATR
      MFE >= 1.0 * ATR                    : Distance = 0.30 * ATR
      MFE >= 1.5 * ATR                    : Distance = 0.25 * ATR
  - Trailing exit triggers ONLY when the 1m CLOSE breaks the trailing level
    (Long: close < trailing; Short: close > trailing) -- never on tick data.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from runner.strategies.xau_v3_mfe_atr.strategy import (
    CandleSeries,
    SupertrendXauV4MfeAtrRunnerStrategy,
    get_mfe_atr_distance,
    process_long_minute_v4,
    process_short_minute_v4,
)


# ------------------------------------------------------- MFE ATR distance


def test_distance_none_below_half_atr() -> None:
    assert (
        get_mfe_atr_distance(mfe_profit=49.0, atr_val=100.0, position_age_min=5) is None
    )


def test_distance_requires_age_2_below_one_atr() -> None:
    # MFE 60 in [0.5, 1.0)*ATR -> needs age >= 2 min.
    assert (
        get_mfe_atr_distance(mfe_profit=60.0, atr_val=100.0, position_age_min=1) is None
    )
    assert get_mfe_atr_distance(
        mfe_profit=60.0, atr_val=100.0, position_age_min=2
    ) == pytest.approx(0.35 * 100.0)


def test_distance_30pct_between_one_and_one_half() -> None:
    assert get_mfe_atr_distance(
        mfe_profit=120.0, atr_val=100.0, position_age_min=0
    ) == pytest.approx(0.30 * 100.0)


def test_distance_25pct_at_or_above_one_and_half() -> None:
    assert get_mfe_atr_distance(
        mfe_profit=160.0, atr_val=100.0, position_age_min=0
    ) == pytest.approx(0.25 * 100.0)


def test_distance_zero_atr_none() -> None:
    assert (
        get_mfe_atr_distance(mfe_profit=60.0, atr_val=0.0, position_age_min=5) is None
    )


# ------------------------------------------------------- Long minute processing


def test_long_no_exit_with_no_trailing() -> None:
    # best_high 140 -> mfe 40 < 0.5*ATR(100)=50 -> no trailing level yet.
    exited, exit_price, best, nxt = process_long_minute_v4(
        minute_open=106.0,
        minute_high=140.0,
        minute_low=105.0,
        minute_close=139.0,
        entry_price=100.0,
        best_high=102.0,
        active_trailing=None,
        atr_val=100.0,
        position_age_min=5,
    )
    assert not exited
    assert best == pytest.approx(140.0)
    assert nxt is None


def test_long_exits_on_close_below_trailing() -> None:
    # Active trailing 115.0; 1m close 114.5 < trailing -> exit at close.
    exited, exit_price, _, nxt = process_long_minute_v4(
        minute_open=116.0,
        minute_high=117.0,
        minute_low=114.0,
        minute_close=114.5,
        entry_price=100.0,
        best_high=140.0,
        active_trailing=115.0,
        atr_val=100.0,
        position_age_min=5,
    )
    assert exited
    assert exit_price == pytest.approx(114.5 * (1 - 0.0001))
    assert nxt is None


def test_long_does_not_exit_on_intrabar_low_below_trailing() -> None:
    # Trailing is close-based: an intrabar low below the trailing level does NOT
    # trigger an exit when the 1m close is still above it (no tick matching).
    exited, exit_price, best, nxt = process_long_minute_v4(
        minute_open=116.0,
        minute_high=117.0,
        minute_low=114.0,
        minute_close=115.5,
        entry_price=100.0,
        best_high=140.0,
        active_trailing=115.0,
        atr_val=100.0,
        position_age_min=5,
    )
    assert not exited
    assert best == pytest.approx(140.0)
    assert nxt == pytest.approx(max(115.0, 140.0 - 0.30 * 100.0))  # ratchet


def test_long_trailing_ratchet_never_loosens() -> None:
    _, _, _, nxt = process_long_minute_v4(
        minute_open=136.0,
        minute_high=160.0,
        minute_low=135.0,
        minute_close=158.0,
        entry_price=100.0,
        best_high=160.0,
        active_trailing=135.0,
        atr_val=100.0,
        position_age_min=5,
    )
    assert nxt == pytest.approx(160.0 - 0.25 * 100.0)  # 135.0, ratchet keeps max


# ------------------------------------------------------- Short minute processing


def test_short_no_exit_with_no_trailing() -> None:
    # best_low 40 -> mfe 60 in [0.5,1.0)*ATR -> distance 0.35*ATR (age>=2).
    exited, exit_price, best, nxt = process_short_minute_v4(
        minute_open=94.0,
        minute_high=94.5,
        minute_low=40.0,
        minute_close=41.0,
        entry_price=100.0,
        best_low=95.0,
        active_trailing=None,
        atr_val=100.0,
        position_age_min=5,
    )
    assert not exited
    assert best == pytest.approx(40.0)
    assert nxt == pytest.approx(40.0 + 0.35 * 100.0)  # 75.0


def test_short_exits_on_close_above_trailing() -> None:
    exited, exit_price, _, nxt = process_short_minute_v4(
        minute_open=64.0,
        minute_high=66.0,
        minute_low=63.0,
        minute_close=65.5,
        entry_price=100.0,
        best_low=40.0,
        active_trailing=65.0,
        atr_val=100.0,
        position_age_min=5,
    )
    assert exited
    assert exit_price == pytest.approx(65.5 * (1 + 0.0001))
    assert nxt is None


def test_short_does_not_exit_on_intrabar_high_above_trailing() -> None:
    exited, _, best, nxt = process_short_minute_v4(
        minute_open=64.0,
        minute_high=66.0,
        minute_low=63.0,
        minute_close=64.5,
        entry_price=100.0,
        best_low=40.0,
        active_trailing=65.0,
        atr_val=100.0,
        position_age_min=5,
    )
    assert not exited
    assert best == pytest.approx(40.0)
    assert nxt == pytest.approx(min(65.0, 40.0 + 0.30 * 100.0))  # ratchet


# ------------------------------------------------------------------ wiring


def _strategy(ctx=None) -> SupertrendXauV4MfeAtrRunnerStrategy:
    strategy = object.__new__(SupertrendXauV4MfeAtrRunnerStrategy)
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
    strategy.alpha_id = "xau-v3-mfe-atr"
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
    assert SupertrendXauV4MfeAtrRunnerStrategy.get_required_channels({}) == [
        "kline:15m",
        "kline:1m",
    ]


def test_warmup_tfs_include_1m() -> None:
    strategy = _strategy()
    assert strategy.get_warmup_tfs() == ["15m", "1m"]
    assert strategy.get_warmup_bars("15m") == 320
    assert strategy.get_warmup_bars("1m") == 60


@pytest.mark.asyncio
async def test_1m_close_break_emits_close_with_executable_ref() -> None:
    series = CandleSeries((116.0,), (117.0,), (114.0,), (114.5,), (_BASE_MS,))
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
            "best_high": 140.0,
            "active_trailing": 115.0,
            "entry_atr": 100.0,
            "entry_candle_open_ms": _BASE_MS - 5 * 60_000,
        }
    }
    await strategy._scan_1m(_BASE_MS)

    assert ctx.emit_signal.await_count == 1
    call = ctx.emit_signal.await_args
    assert call.args[0] == "CLOSE"
    assert call.kwargs["reason"] == "V4_MFE_ATR_TRAILING_CLOSE_EXIT"
    assert call.kwargs["exit_price"] == pytest.approx(114.5 * (1 - 0.0001))
    assert json.loads(call.kwargs["metadata"])["ref_is_executable"] is True
    assert strategy._positions == {}

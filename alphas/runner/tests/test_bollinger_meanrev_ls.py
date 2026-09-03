"""Correctness coverage for the bollinger_meanrev_ls runner strategy (L+S flip).

Reference spec (Pine ``Boll MeanRev L+S Flip``):

    length = 20                          # BB / SMA / stdev window
    basis  = sma(close, length)
    dev    = stdev(close, length)        # sample stdev
    prevZ  = z-score of the previous bar close (nz-guarded to 0 when dev=0)

Semantics:
  - Zone is derived from the closed 1d prefix ``closes[:-1]`` (this is ``prevZ``):
      prevZ < -2.0               -> BUY   (zone==1)
      prevZ >  0.0               -> SELL  (zone==-1)
      -2.0 <= prevZ <= 0.0       -> BETWEEN (zone==0)
  - Flat + BUY     -> open LONG.
  - Flat + SELL    -> open SHORT.
  - LONG + SELL    -> close LONG (FLIP) + open SHORT.
  - SHORT + BUY    -> close SHORT (FLIP) + open LONG.
  - SHORT + BETWEEN-> close SHORT (CASH).
  - LONG + BETWEEN -> hold LONG.
  - bar year < start_year and in position -> close all (OOR).

The closed prefix is ``closes[:-1]``; the trigger close is therefore the
second-to-last bar and the last bar's open is the executable fill price.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from runner.strategies.bollinger_meanrev_ls.strategy import (
    D1_MS,
    ZONE_BETWEEN,
    ZONE_BUY,
    ZONE_SELL,
    BollingerMeanRevLsRunnerStrategy,
    _zscore,
    _zone_from_closes,
)

_BASE_MS = 1_786_000_000_000  # realistic ms epoch > 1e12 (not treated as seconds)


# ------------------------------------------------------------- indicator math


def test_zscore_below_minus_two_for_last_close_below_lower_band() -> None:
    closes = tuple([100.0] * 19 + [50.0])
    z = _zscore(closes, period=20)
    assert z is not None
    assert z < -2.0


def test_zscore_zero_when_close_at_mean() -> None:
    closes = tuple([100.0] * 20)
    z = _zscore(closes, period=20)
    assert z == 0.0


def test_zscore_positive_when_close_above_mean() -> None:
    closes = tuple([100.0] * 19 + [150.0])
    z = _zscore(closes, period=20)
    assert z is not None
    assert z > 0.0


def test_zscore_requires_period_bars() -> None:
    assert _zscore((1.0, 2.0, 3.0), period=20) is None


def test_zone_returns_none_when_not_warmed() -> None:
    assert _zone_from_closes((1.0, 2.0, 3.0), period=20) is None


def test_zone_buy_when_z_below_minus_two() -> None:
    closes = tuple([100.0] * 19 + [50.0])
    assert _zone_from_closes(closes, period=20) == ZONE_BUY


def test_zone_sell_when_z_positive() -> None:
    closes = tuple([100.0] * 19 + [150.0])
    assert _zone_from_closes(closes, period=20) == ZONE_SELL


def test_zone_between_when_close_near_mean() -> None:
    closes = tuple([100.0] * 20)
    assert _zone_from_closes(closes, period=20) == ZONE_BETWEEN


# ------------------------------------------------------------------ wiring


def _cache_with(
    closes: tuple[float, ...], opens: tuple[float, ...] | None = None
) -> SimpleNamespace:
    if opens is None:
        opens = closes

    class _Cache:
        def snapshot(self, symbol, tf, bars):
            return SimpleNamespace(
                opens=opens,
                highs=closes,
                lows=closes,
                closes=closes,
                times=tuple(_BASE_MS + i * D1_MS for i in range(len(closes))),
            )

    return SimpleNamespace(snapshot=_Cache().snapshot)


def _make_ctx(closes, emit, opens=None) -> SimpleNamespace:
    return SimpleNamespace(
        emit_signal=emit,
        load_authoritative_positions=lambda: None,
        load_positions=lambda: None,
        state=SimpleNamespace(ready=True),
        price_alerts=None,
        cache=_cache_with(closes, opens),
    )


def _scan_task(strategy, closes):
    strategy._last_1d_open = _BASE_MS + (len(closes) - 2) * D1_MS
    return strategy._scan_1d(_BASE_MS + (len(closes) - 1) * D1_MS)


def _strategy(ctx=None) -> BollingerMeanRevLsRunnerStrategy:
    strategy = object.__new__(BollingerMeanRevLsRunnerStrategy)
    strategy.bb_period = 20
    strategy.z_entry_buy = -2.0
    strategy.z_entry_sell = 0.0
    strategy.symbol = "FILUSDT"
    strategy.exchange = "binance"
    strategy.capital = 10_000.0
    strategy.leverage = 10.0
    strategy.position_fraction = 1.0
    strategy.fee_pct = 0.0005
    strategy.start_year = 2024
    strategy.d1_warmup_bars = 60
    strategy.retain_bars = 60
    strategy.min_d1_bars = 24
    strategy.timestamp_semantics = "open"
    strategy.alpha_id = "bollinger-meanrev-ls-fil"
    strategy.version = "1"
    strategy._last_1d_open = None
    strategy._pending_1d_open = None
    strategy._positions = {}
    if ctx is None:
        ctx = _make_ctx(tuple([100.0] * 60), AsyncMock(return_value={"ok": True}))
    strategy.ctx = ctx
    return strategy


def ctx_for(closes, emit, opens: tuple[float, ...] | None = None) -> SimpleNamespace:
    return _make_ctx(closes, emit, opens)


def strategy_with_long(ctx) -> BollingerMeanRevLsRunnerStrategy:
    strategy = _strategy(ctx)
    strategy.ctx = ctx
    strategy._positions = {
        "p1": {
            "position_id": "p1",
            "symbol": "FILUSDT",
            "side": "LONG",
            "entry": 100.0,
            "qty": 1.0,
        }
    }
    return strategy


def strategy_with_short(ctx) -> BollingerMeanRevLsRunnerStrategy:
    strategy = _strategy(ctx)
    strategy.ctx = ctx
    strategy._positions = {
        "p1": {
            "position_id": "p1",
            "symbol": "FILUSDT",
            "side": "SHORT",
            "entry": 100.0,
            "qty": 1.0,
        }
    }
    return strategy


def ctx_signals(strategy) -> list[tuple]:
    return [(c.args[0], c.kwargs) for c in strategy.ctx.emit_signal.await_args_list]


def test_requests_1d_channel() -> None:
    assert BollingerMeanRevLsRunnerStrategy.get_required_channels(
        {"symbol": "FILUSDT"}
    ) == ["kline:1d"]


def test_warmup_tfs_and_bars() -> None:
    strategy = _strategy()
    assert strategy.get_warmup_symbols() == ["FILUSDT"]
    assert strategy.get_warmup_tfs() == ["1d"]
    assert strategy.get_warmup_bars("1d") == 60


def test_registry_registers_strategy() -> None:
    from runner.strategy.registry import StrategyRegistry

    from runner.strategies.bollinger_meanrev_ls import register

    registry = StrategyRegistry()
    register(registry)
    assert "bollinger_meanrev_ls" in registry.names()
    assert (
        registry.get_class("bollinger_meanrev_ls") is BollingerMeanRevLsRunnerStrategy
    )


# ------------------------------------------------------------- entry on flat


@pytest.mark.asyncio
async def test_no_position_buy_zone_opens_long() -> None:
    closes = tuple([100.0] * 38 + [50.0, 52.0])  # [...,50] dip -> BUY, fill at 52
    strategy = _strategy(ctx_for(closes, AsyncMock(return_value={"ok": True})))
    await _scan_task(strategy, closes)

    assert ctx_signals(strategy)[0][0] == "OPEN"
    assert ctx_signals(strategy)[0][1]["side"] == "LONG"
    assert ctx_signals(strategy)[0][1]["entry"] == 52.0
    assert len(strategy._positions) == 1


@pytest.mark.asyncio
async def test_no_position_sell_zone_opens_short() -> None:
    closes = tuple([100.0] * 38 + [150.0, 148.0])  # [...,150] spike -> SELL -> SHORT
    strategy = _strategy(ctx_for(closes, AsyncMock(return_value={"ok": True})))
    await _scan_task(strategy, closes)

    assert ctx_signals(strategy)[0][0] == "OPEN"
    assert ctx_signals(strategy)[0][1]["side"] == "SHORT"
    assert ctx_signals(strategy)[0][1]["entry"] == 148.0
    assert len(strategy._positions) == 1


@pytest.mark.asyncio
async def test_no_position_between_zone_stays_flat() -> None:
    closes = tuple([100.0] * 40)  # close exactly at mean -> BETWEEN
    strategy = _strategy(ctx_for(closes, AsyncMock(return_value={"ok": True})))
    await _scan_task(strategy, closes)

    assert ctx_signals(strategy) == []
    assert strategy._positions == {}


# ------------------------------------------------------------- holding LONG rules


@pytest.mark.asyncio
async def test_long_flips_to_short_on_sell_zone() -> None:
    closes = tuple([100.0] * 38 + [150.0, 148.0])  # recovery above MA -> SELL
    strategy = strategy_with_long(ctx_for(closes, AsyncMock(return_value={"ok": True})))
    await _scan_task(strategy, closes)

    calls = ctx_signals(strategy)
    assert [c[0] for c in calls] == ["CLOSE", "OPEN"]
    assert calls[0][1]["reason"] == "FLIP"
    assert calls[1][1]["side"] == "SHORT"
    assert len(strategy._positions) == 1


@pytest.mark.asyncio
async def test_long_held_in_buy_zone() -> None:
    closes = tuple([100.0] * 38 + [50.0, 52.0])  # still BUY -> hold, already in
    strategy = strategy_with_long(ctx_for(closes, AsyncMock(return_value={"ok": True})))
    await _scan_task(strategy, closes)

    assert ctx_signals(strategy) == []
    assert len(strategy._positions) == 1


@pytest.mark.asyncio
async def test_long_held_in_between_zone() -> None:
    closes = tuple([100.0] * 40)  # close at mean -> BETWEEN -> hold long
    strategy = strategy_with_long(ctx_for(closes, AsyncMock(return_value={"ok": True})))
    await _scan_task(strategy, closes)

    assert ctx_signals(strategy) == []
    assert len(strategy._positions) == 1


# ------------------------------------------------------------- holding SHORT rules


@pytest.mark.asyncio
async def test_short_flips_to_long_on_buy_zone() -> None:
    closes = tuple([100.0] * 38 + [50.0, 52.0])  # oversold -> BUY zone
    strategy = strategy_with_short(
        ctx_for(closes, AsyncMock(return_value={"ok": True}))
    )
    await _scan_task(strategy, closes)

    calls = ctx_signals(strategy)
    assert [c[0] for c in calls] == ["CLOSE", "OPEN"]
    assert calls[0][1]["reason"] == "FLIP"
    assert calls[1][1]["side"] == "LONG"
    assert len(strategy._positions) == 1


@pytest.mark.asyncio
async def test_short_closed_to_cash_on_between_zone() -> None:
    closes = tuple([100.0] * 40)  # close exactly at mean -> BETWEEN
    strategy = strategy_with_short(
        ctx_for(closes, AsyncMock(return_value={"ok": True}))
    )
    await _scan_task(strategy, closes)

    calls = ctx_signals(strategy)
    assert [c[0] for c in calls] == ["CLOSE"]
    assert calls[0][1]["reason"] == "CASH"
    assert strategy._positions == {}


@pytest.mark.asyncio
async def test_short_held_in_sell_zone() -> None:
    closes = tuple([100.0] * 38 + [150.0, 148.0])  # SELL zone -> hold short
    strategy = strategy_with_short(
        ctx_for(closes, AsyncMock(return_value={"ok": True}))
    )
    await _scan_task(strategy, closes)

    assert ctx_signals(strategy) == []
    assert len(strategy._positions) == 1


# ------------------------------------------------------------- out-of-range guard


@pytest.mark.asyncio
async def test_out_of_range_closes_all() -> None:
    closes = tuple([100.0] * 38 + [50.0, 52.0])  # BUY zone
    strategy = strategy_with_long(ctx_for(closes, AsyncMock(return_value={"ok": True})))
    strategy.start_year = 1_000_000  # every bar year < start_year -> OOR
    await _scan_task(strategy, closes)

    calls = ctx_signals(strategy)
    assert [c[0] for c in calls] == ["CLOSE"]
    assert calls[0][1]["reason"] == "OOR"
    assert strategy._positions == {}


@pytest.mark.asyncio
async def test_out_of_range_flat_stays_flat() -> None:
    closes = tuple([100.0] * 38 + [50.0, 52.0])  # BUY zone
    strategy = _strategy(ctx_for(closes, AsyncMock(return_value={"ok": True})))
    strategy.start_year = 1_000_000
    await _scan_task(strategy, closes)

    assert ctx_signals(strategy) == []
    assert strategy._positions == {}

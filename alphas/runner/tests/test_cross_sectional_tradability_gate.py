"""Risk gate: block new OPENs on symbols MDS no longer reports as TRADING.

Regression coverage for the 2026-07-15 incident where 1d-iamp re-opened
TONUSDT/IPUSDT weeks after Binance moved them to SETTLING, because nothing
checked live tradability before emitting OPEN signals.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from cross_alpha.strategy import Selection
from runner.strategies.cross_sectional.strategy import CrossSectionalRunnerStrategy


class _FakeSpec:
    timeframe = "1d"
    fee_bps = 7.0
    vol_lookback = 30
    max_leverage = 3.0
    target_vol = 0.1
    ppy = 365


def _make_ctx(live_tradable_symbols):
    ctx = MagicMock()
    ctx.state.lease_valid = True
    ctx.can_open_trades.return_value = True
    ctx.emit_signal = AsyncMock(return_value={"ok": True})
    ctx.save_positions = MagicMock()
    ctx.live_tradable_symbols = live_tradable_symbols
    return ctx


def _make_strategy(live_tradable_symbols, open_positions=None):
    strat = CrossSectionalRunnerStrategy.__new__(CrossSectionalRunnerStrategy)
    strat.alpha_id = "test-alpha"
    strat.ctx = _make_ctx(live_tradable_symbols)
    strat.spec = _FakeSpec()
    strat.exchange = "binance"
    strat.capital = 10_000.0
    strat._strategy_leverage = 1.0
    strat._base_weights = {}
    strat._pending_cost = 0.0
    strat._portfolio_returns = []
    strat._open_positions = open_positions or {}
    return strat


def _selection(weights: dict[str, float]) -> Selection:
    longs = sorted(s for s, w in weights.items() if w > 0)
    shorts = sorted(s for s, w in weights.items() if w < 0)
    return Selection(longs=longs, shorts=shorts, scores={}, ranks={}, weights=weights,
                      indicators={}, diagnostics={})


@pytest.mark.asyncio
async def test_apply_selection_skips_open_for_non_tradable_symbol():
    # ETHUSDT dropped from MDS's live universe (e.g. moved to SETTLING) --
    # BTCUSDT is still tradable.
    strategy = _make_strategy(live_tradable_symbols={"BTCUSDT"})
    selection = _selection({"BTCUSDT": 0.5, "ETHUSDT": -0.5})
    prices = {"BTCUSDT": 100.0, "ETHUSDT": 200.0}

    await strategy._apply_selection(selection, prices, candle_open_ms=1_000)

    open_calls = [c for c in strategy.ctx.emit_signal.await_args_list if c.args[0] == "OPEN"]
    opened_symbols = {c.kwargs["symbol"] for c in open_calls}
    assert opened_symbols == {"BTCUSDT"}
    assert "ETHUSDT" not in strategy._open_positions
    assert "BTCUSDT" in strategy._open_positions


@pytest.mark.asyncio
async def test_apply_selection_fails_open_when_tradable_universe_unknown():
    # live_tradable_symbols is None until the first `symbols:{exchange}`
    # broadcast arrives -- must not block everything in the meantime.
    strategy = _make_strategy(live_tradable_symbols=None)
    selection = _selection({"BTCUSDT": 0.5, "ETHUSDT": -0.5})
    prices = {"BTCUSDT": 100.0, "ETHUSDT": 200.0}

    await strategy._apply_selection(selection, prices, candle_open_ms=1_000)

    open_calls = [c for c in strategy.ctx.emit_signal.await_args_list if c.args[0] == "OPEN"]
    opened_symbols = {c.kwargs["symbol"] for c in open_calls}
    assert opened_symbols == {"BTCUSDT", "ETHUSDT"}


@pytest.mark.asyncio
async def test_apply_selection_still_closes_existing_position_on_non_tradable_symbol():
    # The gate only blocks new OPENs (per the approved plan) -- an existing
    # open position on a now non-tradable symbol must still get its
    # REBALANCE close emitted like any other held position.
    existing = {
        "TONUSDT": {
            "position_id": "pos-1", "symbol": "TONUSDT", "side": "LONG",
            "entry": 1.59, "qty": 10.0, "weight": 0.1, "strategy_leverage": 1.0,
        },
    }
    strategy = _make_strategy(live_tradable_symbols={"BTCUSDT"}, open_positions=existing)
    selection = _selection({"BTCUSDT": 1.0})
    prices = {"BTCUSDT": 100.0}

    await strategy._apply_selection(selection, prices, candle_open_ms=1_000)

    close_calls = [c for c in strategy.ctx.emit_signal.await_args_list if c.args[0] == "CLOSE"]
    assert {c.kwargs["symbol"] for c in close_calls} == {"TONUSDT"}


def test_get_required_channels_instance_subscribes_to_symbols_broadcast():
    strategy = CrossSectionalRunnerStrategy.__new__(CrossSectionalRunnerStrategy)
    strategy.params = {}
    strategy.exchange = "binance"

    channels = strategy.get_required_channels_instance()

    assert "symbols:binance" in channels


def test_get_required_channels_classmethod_unaffected_by_symbols_channel():
    # _tf_set_from_strategy() in main.py calls the classmethod directly and
    # does `ch.replace("kline:", "")` on every entry to build a tf set --
    # the symbols:{exchange} channel must stay out of it (see comment in
    # get_required_channels_instance) or it would be misparsed as a timeframe.
    channels = CrossSectionalRunnerStrategy.get_required_channels({})
    assert all(ch.startswith("kline:") for ch in channels)

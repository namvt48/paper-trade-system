"""Regression tests: daily cross-sectional alphas must keep rebalancing even when
market data is partial, and must CLOSE any held position whose symbol has no data
at the rebalance bar.

Covers the 2026-08-21 incident (1d-iamp positions open 27h-75h past the 24h
rebalance):
- H1 orphan: the strategy adopts authoritative positions it lost on an earlier
  startup and closes them at rebalance (previously the close loop only saw the
  in-memory set, so orphaned DB positions were never closed -- and with no
  TP/SL they stayed open forever).
- H2 coverage gate: a rebalance bar with held positions must not be blocked by
  the full-universe kline coverage threshold (95% of 197 symbols); data-less
  symbols are closed via entry-price fallback instead of freezing the rebalance.
- readiness gate: the rebalance scan must run even when full-universe readiness
  is False, as long as positions are held and the bar is a rebalance bar.
- cache-latest cap: a single far-ahead symbol must not advance the scanned
  candle and trigger an early rebalance on a stale panel.
- watchdog: the per-event timeout ceiling for 1d events must allow slow daily
  scans to complete instead of abandoning them.
"""

from __future__ import annotations

import pandas as pd
import pytest
from unittest.mock import AsyncMock, MagicMock

from cross_alpha.strategy import Selection
from runner.strategies.cross_sectional.strategy import CrossSectionalRunnerStrategy

DAY_MS = 86_400_000


class _FakeSpec:
    alpha_id = "test-alpha"
    timeframe = "1d"
    rebalance_bars = 1
    publish_at_midnight_utc = False
    rebalance_on_close = False
    signal = "ideal_amplitude"
    params: dict = {}
    long_threshold = 0.0
    short_threshold = 0.0
    fee_bps = 7.0
    vol_lookback = 30
    max_leverage = 3.0
    target_vol = 0.1
    ppy = 365


def _make_ctx(**overrides):
    ctx = MagicMock()
    ctx.state.lease_valid = True
    ctx.state.ready = True
    ctx.can_open_trades.return_value = True
    ctx.emit_signal = AsyncMock(return_value={"ok": True})
    ctx.save_positions = MagicMock()
    ctx.live_tradable_symbols = None
    ctx.panel_feature_cache = MagicMock()
    ctx.load_authoritative_positions.return_value = {}
    # Candle-cache lookups return "no cached data" unless a test overrides.
    ctx.cache.get_closes.return_value = ()
    for key, value in overrides.items():
        parts = key.split(".")
        obj = ctx
        for part in parts[:-1]:
            obj = getattr(obj, part)
        setattr(obj, parts[-1], value)
    return ctx


def _make_strategy(open_positions=None, **overrides):
    strat = CrossSectionalRunnerStrategy.__new__(CrossSectionalRunnerStrategy)
    strat.alpha_id = "test-alpha"
    strat.ctx = _make_ctx(**overrides.pop("ctx_overrides", {}))
    strat.spec = _FakeSpec()
    strat.exchange = "binance"
    strat.capital = 10_000.0
    strat._strategy_leverage = 1.0
    strat._base_weights = {}
    strat._pending_cost = 0.0
    strat._portfolio_returns = []
    strat._portfolio_returns_min_length = 0
    strat._open_positions = open_positions or {}
    strat.book_only = False
    strat._book_store = None
    strat._last_prices = {}
    strat._equity = 1.0
    strat._peak_equity = 1.0
    strat._shadow_equity = 1.0
    strat._symbols = ["XUSDT"]
    strat._symbol_set = {"XUSDT"}
    strat.scan_min_symbol_coverage = 0.95
    strat._warmup_complete = True
    strat._last_processed_candle = 0
    for key, value in overrides.items():
        setattr(strat, key, value)
    return strat


def _selection(weights: dict[str, float]) -> Selection:
    longs = sorted(s for s, w in weights.items() if w > 0)
    shorts = sorted(s for s, w in weights.items() if w < 0)
    return Selection(
        longs=longs,
        shorts=shorts,
        scores={},
        ranks={},
        weights=weights,
        indicators={},
        diagnostics={},
    )


@pytest.mark.asyncio
async def test_apply_selection_closes_orphaned_authoritative_positions():
    """H1: positions the worker DB still owns but the strategy lost on a
    previous startup must be re-adopted at rebalance and CLOSEd -- with
    entry-price fallback when the symbol has no price in the current panel."""
    orphan = {
        "position_id": "orph-pid",
        "symbol": "ORPHUSDT",
        "side": "LONG",
        "entry": 1.0,
        "qty": 10.0,
        "weight": 0.1,
    }
    strategy = _make_strategy(
        open_positions={},
        ctx_overrides={
            "load_authoritative_positions.return_value": {"orph-pid": orphan},
        },
    )
    selection = _selection({"BTCUSDT": 1.0})
    prices = {"BTCUSDT": 100.0}

    await strategy._apply_selection(selection, prices, candle_open_ms=1_000)

    close_calls = [
        c for c in strategy.ctx.emit_signal.await_args_list if c.args[0] == "CLOSE"
    ]
    assert [c.kwargs["symbol"] for c in close_calls] == ["ORPHUSDT"]
    assert close_calls[0].kwargs["position_id"] == "orph-pid"
    # No data for ORPHUSDT at rebalance -> closed immediately at entry price.
    assert close_calls[0].kwargs["exit_price"] == 1.0


@pytest.mark.asyncio
async def test_apply_selection_adopts_orphan_alongside_held_positions():
    """H1: in-memory positions and adopted orphans are closed in one pass."""
    held = {
        "XUSDT": {
            "position_id": "held-pid",
            "symbol": "XUSDT",
            "side": "LONG",
            "entry": 50.0,
            "qty": 1.0,
            "weight": 0.5,
        },
    }
    orphan = {
        "position_id": "orph-pid",
        "symbol": "ORPHUSDT",
        "side": "SHORT",
        "entry": 2.0,
        "qty": 5.0,
        "weight": -0.5,
    }
    strategy = _make_strategy(
        open_positions=held,
        ctx_overrides={
            "load_authoritative_positions.return_value": {"orph-pid": orphan},
        },
    )
    selection = _selection({"XUSDT": 1.0})
    prices = {"XUSDT": 55.0}

    await strategy._apply_selection(selection, prices, candle_open_ms=1_000)

    close_calls = [
        c for c in strategy.ctx.emit_signal.await_args_list if c.args[0] == "CLOSE"
    ]
    by_symbol = {c.kwargs["symbol"]: c.kwargs for c in close_calls}
    assert set(by_symbol) == {"XUSDT", "ORPHUSDT"}
    # Held position exits at its live price; orphan at entry fallback when the
    # symbol has no panel data and no cached close (worst case).
    assert by_symbol["XUSDT"]["exit_price"] == 55.0
    assert by_symbol["ORPHUSDT"]["exit_price"] == 2.0


@pytest.mark.asyncio
async def test_orphan_close_uses_latest_cached_close_when_panel_data_missing():
    """Exit price must be the NEAREST price cache, never a stale entry.

    A symbol with no data at the rebalance bar still has a last known close in
    the shared candle cache. The CLOSE must carry that cached close (the
    freshest price available) rather than the entry price from days ago."""
    orphan = {
        "position_id": "orph-pid",
        "symbol": "ORPHUSDT",
        "side": "LONG",
        "entry": 1.0,  # stale -- opened days ago
        "qty": 10.0,
        "weight": 0.1,
    }
    strategy = _make_strategy(
        open_positions={},
        ctx_overrides={
            "load_authoritative_positions.return_value": {"orph-pid": orphan},
            # The shared candle cache still holds ORPHUSDT's most recent close.
            "cache.get_closes.return_value": (99.5,),
        },
    )
    selection = _selection({"BTCUSDT": 1.0})
    prices = {"BTCUSDT": 100.0}

    await strategy._apply_selection(selection, prices, candle_open_ms=1_000)

    close_calls = [
        c for c in strategy.ctx.emit_signal.await_args_list if c.args[0] == "CLOSE"
    ]
    assert close_calls[0].kwargs["symbol"] == "ORPHUSDT"
    assert close_calls[0].kwargs["exit_price"] == 99.5


@pytest.mark.asyncio
async def test_orphan_close_falls_back_to_last_prices_when_cache_empty():
    """If neither the panel nor the candle cache holds the symbol, the most
    recent observed price (_last_prices) is still fresher than entry."""
    orphan = {
        "position_id": "orph-pid",
        "symbol": "ORPHUSDT",
        "side": "LONG",
        "entry": 1.0,
        "qty": 10.0,
        "weight": 0.1,
    }
    strategy = _make_strategy(
        open_positions={},
        ctx_overrides={
            "load_authoritative_positions.return_value": {"orph-pid": orphan},
        },
    )
    strategy._last_prices = {"ORPHUSDT": 98.25}  # last observed before the gap
    selection = _selection({"BTCUSDT": 1.0})
    prices = {"BTCUSDT": 100.0}

    await strategy._apply_selection(selection, prices, candle_open_ms=1_000)

    close_calls = [
        c for c in strategy.ctx.emit_signal.await_args_list if c.args[0] == "CLOSE"
    ]
    assert close_calls[0].kwargs["exit_price"] == 98.25


def test_should_scan_allows_rebalance_bar_with_held_positions_on_low_coverage():
    """H2: with open positions on a rebalance bar, partial data must NOT block
    the scan -- data-less symbols need to be closed now."""
    strategy = _make_strategy(
        open_positions={"XUSDT": {"position_id": "p1", "side": "LONG"}},
        coverage=0.30,
    )
    strategy.ctx.cache.get_latest_timestamp.return_value = DAY_MS * 10
    strategy._last_processed_candle = DAY_MS * 9
    strategy._candle_coverage = MagicMock(return_value=0.30)  # type: ignore[method-assign]

    assert strategy.should_scan_after_event("kline", "XUSDT", "1d") is True


def test_should_scan_still_blocks_rebalance_bar_when_flat_on_low_coverage():
    """H2: the relaxed gate only applies to alphas that hold positions; a flat
    alpha still waits for full coverage before opening a new book."""
    strategy = _make_strategy(open_positions={}, coverage=0.30)
    strategy.ctx.cache.get_latest_timestamp.return_value = DAY_MS * 10
    strategy._last_processed_candle = DAY_MS * 9
    strategy._candle_coverage = MagicMock(return_value=0.30)  # type: ignore[method-assign]

    assert strategy.should_scan_after_event("kline", "XUSDT", "1d") is False


@pytest.mark.asyncio
async def test_scan_runs_rebalance_when_readiness_false_but_positions_held():
    """Readiness gate: a rebalance bar with held positions must proceed even
    when full-universe readiness is False (partial data)."""
    strategy = _make_strategy(
        open_positions={"XUSDT": {"position_id": "p1", "side": "LONG"}},
    )
    strategy.ctx.state.ready = False
    strategy._last_processed_candle = DAY_MS * 9

    panel = pd.DataFrame(
        {"XUSDT": [100.0]},
        index=pd.Index([DAY_MS * 10], dtype="int64"),
    )
    bundle = MagicMock()
    bundle.panel = {"close": panel}
    bundle.latest = DAY_MS * 10
    strategy._shared_panel_bundle = AsyncMock(return_value=bundle)
    strategy._latest_cached_timestamp = MagicMock(return_value=DAY_MS * 10)
    strategy._select_positions = MagicMock(return_value=_selection({"XUSDT": 1.0}))
    strategy._apply_selection = AsyncMock()  # type: ignore[method-assign]

    await strategy.scan()

    strategy._apply_selection.assert_awaited_once()
    assert strategy._last_processed_candle == DAY_MS * 10


@pytest.mark.asyncio
async def test_scan_skips_when_readiness_false_and_flat():
    """Readiness gate: a flat alpha with low readiness still does not trade."""
    strategy = _make_strategy(open_positions={})
    strategy.ctx.state.ready = False
    strategy._last_processed_candle = DAY_MS * 9

    panel = pd.DataFrame(
        {"XUSDT": [100.0]},
        index=pd.Index([DAY_MS * 10], dtype="int64"),
    )
    bundle = MagicMock()
    bundle.panel = {"close": panel}
    bundle.latest = DAY_MS * 10
    strategy._shared_panel_bundle = AsyncMock(return_value=bundle)
    strategy._latest_cached_timestamp = MagicMock(return_value=DAY_MS * 10)
    strategy._apply_selection = AsyncMock()  # type: ignore[method-assign]

    await strategy.scan()

    strategy._apply_selection.assert_not_awaited()


@pytest.mark.asyncio
async def test_scan_does_not_advance_on_far_ahead_outlier_cache_timestamp():
    """cache-latest cap: a single symbol 3 bars ahead of the panel must not
    advance the scanned candle and trigger an early rebalance on a stale
    panel (which would close-and-reopen the whole book with stale prices)."""
    strategy = _make_strategy(
        open_positions={"XUSDT": {"position_id": "p1", "side": "LONG"}},
    )
    strategy.ctx.state.ready = True
    strategy._last_processed_candle = DAY_MS * 10  # panel is at this candle

    panel = pd.DataFrame(
        {"XUSDT": [100.0]},
        index=pd.Index([DAY_MS * 10], dtype="int64"),
    )
    bundle = MagicMock()
    bundle.panel = {"close": panel}
    bundle.latest = DAY_MS * 10
    strategy._shared_panel_bundle = AsyncMock(return_value=bundle)
    # One noisy symbol is 3 bars ahead -- must not crack the gate.
    strategy._latest_cached_timestamp = MagicMock(return_value=DAY_MS * 13)
    strategy._select_positions = MagicMock(  # type: ignore[method-assign]
        return_value=_selection({"XUSDT": 1.0})
    )
    strategy._apply_selection = AsyncMock()  # type: ignore[method-assign]

    await strategy.scan()

    strategy._apply_selection.assert_not_awaited()
    assert strategy._last_processed_candle == DAY_MS * 10


def test_1d_event_timeout_allows_slow_daily_scans():
    """Watchdog: the per-event timeout ceiling for 1d must be generous enough
    for a 197-symbol daily scan (panel + selection) to complete; 120s abandons
    legitimate slow scans and skips the day's rebalance."""
    from runner import main as runner_main

    assert runner_main._EVENT_TIMEOUT_SEC["1d"] >= 300.0

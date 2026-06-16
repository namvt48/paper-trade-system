from __future__ import annotations

from runner.data_layer.cache import SharedCandleCache
from runner.reconcile.state import StrategyRuntimeState
from runner.strategy.context import StrategyContext


def _load(cache, symbol):
    for i in range(2):
        cache.upsert_candle(symbol, "15m", {
            "open_time": 1_000 + i,
            "open": 1,
            "high": 2,
            "low": 0,
            "close": 1,
            "volume": 1,
        })


def test_context_can_open_trades_false_when_any_flag_stale():
    state = StrategyRuntimeState(ready=True)
    ctx = StrategyContext("a", "1", SharedCandleCache(), None, state)
    assert ctx.can_open_trades() is True

    for attr in ("data_stale", "reconcile_stale", "price_alert_stale"):
        setattr(state, attr, True)
        assert ctx.can_open_trades() is False
        setattr(state, attr, False)
    state.lease_valid = False
    assert ctx.can_open_trades() is False


def test_readiness_passes_at_90_percent_and_fails_below():
    cache = SharedCandleCache()
    symbols = [f"S{i}" for i in range(10)]
    for symbol in symbols[:9]:
        _load(cache, symbol)
    ctx = StrategyContext("a", "1", cache, None, StrategyRuntimeState(), 0.90)

    assert ctx.update_readiness(symbols, "15m", 2) is True

    ctx.state.ready = False
    assert ctx.update_readiness(symbols, "15m", 3) is False


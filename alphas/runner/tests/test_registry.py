from __future__ import annotations

import pytest

from runner.data_layer.cache import SharedCandleCache
from runner.reconcile.state import StrategyRuntimeState
from runner.strategy.base import Strategy
from runner.strategy.context import StrategyContext
from runner.strategy.registry import StrategyRegistry


class DummyStrategy(Strategy):
    def get_required_channels(self): return ["kline:binance:15m"]
    def get_warmup_symbols(self): return ["BTCUSDT"]
    def get_warmup_tfs(self): return ["15m"]
    def get_warmup_bars(self, tf): return 2


def make_ctx():
    return StrategyContext("a", "1", SharedCandleCache(), None, StrategyRuntimeState())


def test_registry_register_and_create():
    registry = StrategyRegistry()
    registry.register("dummy", DummyStrategy)

    strategy = registry.create("dummy", "a", "1", {"x": 1}, make_ctx())

    assert isinstance(strategy, DummyStrategy)
    assert strategy.params == {"x": 1}


def test_registry_rejects_duplicate_and_unknown_is_clear():
    registry = StrategyRegistry()
    registry.register("dummy", DummyStrategy)

    with pytest.raises(ValueError, match="already registered"):
        registry.register("dummy", DummyStrategy)
    with pytest.raises(KeyError, match="unknown strategy 'missing'"):
        registry.create("missing", "a", "1", {}, make_ctx())


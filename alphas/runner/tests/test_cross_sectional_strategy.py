from __future__ import annotations

from runner.data_layer.cache import SharedCandleCache
from runner.reconcile.state import StrategyRuntimeState
from runner.strategies.cross_sectional import register
from runner.strategy.context import StrategyContext
from runner.strategy.registry import StrategyRegistry


def test_cross_sectional_strategy_loads_existing_spec_and_declares_requirements():
    registry = StrategyRegistry()
    register(registry)
    cache = SharedCandleCache()
    ctx = StrategyContext(
        alpha_id="15m-trend-close",
        version="1",
        cache=cache,
        signal_dispatcher=None,
        state=StrategyRuntimeState(ready=False),
    )

    strategy = registry.create(
        "cross_sectional",
        "15m-trend-close",
        "1",
        {
            "spec_file": "15m-trend-close/spec.json",
            "universe_file": "15m-trend-close/data/universe.json",
            "blacklist_file": "15m-trend-close/blacklist.txt",
        },
        ctx,
    )

    assert strategy.get_required_channels() == ["kline:15m"]
    assert strategy.get_warmup_tfs() == ["15m"]
    assert strategy.get_warmup_bars("15m") == 8640
    assert strategy.get_retain_bars("15m") == 8640
    assert len(strategy.get_warmup_symbols()) > 100


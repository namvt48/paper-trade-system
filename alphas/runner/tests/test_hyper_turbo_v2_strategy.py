from __future__ import annotations

from runner.data_layer.cache import SharedCandleCache
from runner.reconcile.state import StrategyRuntimeState
from runner.strategies.hyper_turbo_v2 import register
from runner.strategy.context import StrategyContext
from runner.strategy.registry import StrategyRegistry


def test_hyper_turbo_v2_strategy_declares_legacy_runtime_requirements():
    registry = StrategyRegistry()
    register(registry)
    ctx = StrategyContext(
        alpha_id="hyper-turbo-v2",
        version="1",
        cache=SharedCandleCache(),
        signal_dispatcher=None,
        state=StrategyRuntimeState(),
    )

    strategy = registry.create(
        "hyper_turbo_v2",
        "hyper-turbo-v2",
        "1",
        {
            "tf": "4h",
            "warmup_bars": 360,
            "retain_bars": 1000,
            "retain_1m_bars": 120,
        },
        ctx,
    )

    assert strategy.get_required_channels() == ["kline:4h", "kline:1m"]
    assert strategy.get_warmup_tfs() == ["4h", "1m"]
    assert strategy.get_warmup_bars("4h") == 360
    assert strategy.get_warmup_bars("1m") == 1
    assert strategy.get_retain_bars("4h") == 1000
    assert strategy.get_retain_bars("1m") == 120
    assert len(strategy.get_warmup_symbols()) == 19


from __future__ import annotations

from runner.data_layer.cache import SharedCandleCache
from runner.reconcile.state import StrategyRuntimeState
from runner.strategies.v5_tail import register
from runner.strategy.context import StrategyContext
from runner.strategy.registry import StrategyRegistry


def test_v5_tail_strategy_declares_shared_15m_requirement_from_symbols_file():
    registry = StrategyRegistry()
    register(registry)
    ctx = StrategyContext(
        alpha_id="alpha-1-v5b",
        version="1",
        cache=SharedCandleCache(),
        signal_dispatcher=None,
        state=StrategyRuntimeState(),
    )

    strategy = registry.create(
        "v5_tail",
        "alpha-1-v5b",
        "1",
        {
            "tf": "15m",
            "warmup_bars": 400,
            "retain_bars": 400,
            "symbols_file": "alpha-1-v5b/data/binance_futures_leverage.json",
            "leverage_file": "alpha-1-v5b/data/binance_futures_leverage.json",
        },
        ctx,
    )

    assert strategy.get_required_channels() == ["kline:15m"]
    assert strategy.get_warmup_tfs() == ["15m"]
    assert strategy.get_warmup_bars("15m") == 400
    assert strategy.get_retain_bars("15m") == 400
    assert len(strategy.get_warmup_symbols()) > 100


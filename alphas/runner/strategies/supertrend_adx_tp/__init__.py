from __future__ import annotations

from runner.strategy.registry import StrategyRegistry

from .strategy import SupertrendAdxTpRunnerStrategy


def register(registry: StrategyRegistry) -> None:
    registry.register("supertrend_adx_tp", SupertrendAdxTpRunnerStrategy)

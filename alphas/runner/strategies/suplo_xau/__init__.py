from __future__ import annotations

from runner.strategy.registry import StrategyRegistry

from .strategy import SuploXauRunnerStrategy


def register(registry: StrategyRegistry) -> None:
    registry.register("suplo_xau", SuploXauRunnerStrategy)

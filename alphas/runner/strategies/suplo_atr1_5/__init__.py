from __future__ import annotations

from runner.strategy.registry import StrategyRegistry

from .strategy import SuploAtr1RunnerStrategy


def register(registry: StrategyRegistry) -> None:
    registry.register("suplo_atr1_5", SuploAtr1RunnerStrategy)

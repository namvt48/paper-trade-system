from __future__ import annotations

from runner.strategy.registry import StrategyRegistry

from .strategy import CrossSectionalRunnerStrategy


def register(registry: StrategyRegistry) -> None:
    registry.register("cross_sectional", CrossSectionalRunnerStrategy)


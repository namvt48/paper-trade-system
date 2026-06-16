from __future__ import annotations

from runner.strategy.registry import StrategyRegistry

from .strategy import V5TailRunnerStrategy


def register(registry: StrategyRegistry) -> None:
    registry.register("v5_tail", V5TailRunnerStrategy)


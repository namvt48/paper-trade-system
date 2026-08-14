from __future__ import annotations

from runner.strategy.registry import StrategyRegistry

from .strategy import SupertrendXauV3StepwiseRunnerStrategy


def register(registry: StrategyRegistry) -> None:
    registry.register("suplo_xau_v3_stepwise", SupertrendXauV3StepwiseRunnerStrategy)

from __future__ import annotations

from runner.strategy.registry import StrategyRegistry

from .strategy import HyperTurboV2RunnerStrategy


def register(registry: StrategyRegistry) -> None:
    registry.register("hyper_turbo_v2", HyperTurboV2RunnerStrategy)


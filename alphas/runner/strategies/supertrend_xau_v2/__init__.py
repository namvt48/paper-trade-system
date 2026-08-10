from __future__ import annotations

from runner.strategy.registry import StrategyRegistry

from .strategy import SupertrendXauV2RunnerStrategy


def register(registry: StrategyRegistry) -> None:
    registry.register("supertrend_xau_v2", SupertrendXauV2RunnerStrategy)

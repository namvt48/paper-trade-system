from __future__ import annotations

from runner.strategy.registry import StrategyRegistry

from .strategy import XauM30RunnerStrategy


def register(registry: StrategyRegistry) -> None:
    registry.register("xau_m30", XauM30RunnerStrategy)

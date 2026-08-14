from __future__ import annotations

from runner.strategy.registry import StrategyRegistry

from .strategy import SmoothChanRunnerStrategy


def register(registry: StrategyRegistry) -> None:
    registry.register("smooth_chan", SmoothChanRunnerStrategy)

from .strategy import BangocV22RunnerStrategy
from runner.strategy.registry import StrategyRegistry


def register(registry: StrategyRegistry) -> None:
    registry.register("bangoc_v22", BangocV22RunnerStrategy)

from __future__ import annotations

from runner.strategy.registry import StrategyRegistry

from .strategy import BollingerMeanRevLsRunnerStrategy


def register(registry: StrategyRegistry) -> None:
    registry.register("bollinger_meanrev_ls", BollingerMeanRevLsRunnerStrategy)

from __future__ import annotations

from runner.strategy.registry import StrategyRegistry

from .strategy import BollingerMeanRevRunnerStrategy


def register(registry: StrategyRegistry) -> None:
    registry.register("bollinger_meanrev", BollingerMeanRevRunnerStrategy)

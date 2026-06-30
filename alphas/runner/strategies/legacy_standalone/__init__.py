from __future__ import annotations

from runner.strategies.legacy_standalone.strategy import LegacyStandaloneRunnerStrategy
from runner.strategy.registry import StrategyRegistry


def register(registry: StrategyRegistry) -> None:
    registry.register("legacy_standalone", LegacyStandaloneRunnerStrategy)

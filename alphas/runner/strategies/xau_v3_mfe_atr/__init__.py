from __future__ import annotations

from runner.strategy.registry import StrategyRegistry

from .strategy import SupertrendXauV4MfeAtrRunnerStrategy


def register(registry: StrategyRegistry) -> None:
    registry.register("xau_v3_mfe_atr", SupertrendXauV4MfeAtrRunnerStrategy)

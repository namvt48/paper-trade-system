from __future__ import annotations

from runner.strategy.registry import StrategyRegistry

from .strategy import SupertrendAdxEma13TpRunnerStrategy


def register(registry: StrategyRegistry) -> None:
    registry.register("supertrend_adx_ema13_tp", SupertrendAdxEma13TpRunnerStrategy)

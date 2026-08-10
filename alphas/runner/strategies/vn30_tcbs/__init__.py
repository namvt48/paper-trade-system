"""VN30 futures strategies backed by TCBS market data."""

from __future__ import annotations

from runner.strategy.registry import StrategyRegistry

from .strategy import Vn30TcbsRunnerStrategy


def register(registry: StrategyRegistry) -> None:
    registry.register("vn30_tcbs", Vn30TcbsRunnerStrategy)

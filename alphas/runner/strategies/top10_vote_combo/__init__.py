from __future__ import annotations

from runner.strategy.registry import StrategyRegistry

from .strategy import Top10VoteComboRunnerStrategy


def register(registry: StrategyRegistry) -> None:
    registry.register("top10_vote_combo", Top10VoteComboRunnerStrategy)

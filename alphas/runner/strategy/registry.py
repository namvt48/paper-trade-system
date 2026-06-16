from __future__ import annotations

from runner.strategy.base import Strategy
from runner.strategy.context import StrategyContext


class StrategyRegistry:
    def __init__(self):
        self._items: dict[str, type[Strategy]] = {}

    def register(self, name: str, cls: type[Strategy], replace: bool = False) -> None:
        if name in self._items and not replace:
            raise ValueError(f"strategy already registered: {name}")
        self._items[name] = cls

    def create(
        self,
        name: str,
        alpha_id: str,
        version: str,
        params: dict,
        ctx: StrategyContext,
    ) -> Strategy:
        try:
            cls = self._items[name]
        except KeyError as exc:
            known = ", ".join(sorted(self._items)) or "<none>"
            raise KeyError(f"unknown strategy '{name}' (known: {known})") from exc
        return cls(alpha_id=alpha_id, version=version, params=params, ctx=ctx)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._items))


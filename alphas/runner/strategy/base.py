from __future__ import annotations

from abc import ABC, abstractmethod

from runner.strategy.context import StrategyContext


class Strategy(ABC):
    def __init__(self, alpha_id: str, version: str, params: dict, ctx: StrategyContext):
        self.alpha_id = alpha_id
        self.version = version
        self.params = params
        self.ctx = ctx

    @abstractmethod
    def get_required_channels(self) -> list[str]:
        ...

    @abstractmethod
    def get_warmup_symbols(self) -> list[str]:
        ...

    @abstractmethod
    def get_warmup_tfs(self) -> list[str]:
        ...

    @abstractmethod
    def get_warmup_bars(self, tf: str) -> int:
        ...

    def get_retain_bars(self, tf: str) -> int:
        return self.get_warmup_bars(tf)

    def get_retain_buffer_bars(self, tf: str) -> int:
        return 0

    async def on_candle(self, symbol: str, tf: str) -> None:
        return None

    async def on_price_alert(self, symbol: str, price: float, side: str) -> None:
        return None

    async def scan(self) -> None:
        return None

    async def manage_positions(self) -> None:
        return None

from __future__ import annotations

from dataclasses import dataclass

from runner.data_layer.cache import SharedCandleCache
from runner.reconcile.state import StrategyRuntimeState


@dataclass
class PriceAlertProxy:
    symbols: set[str]

    def sync(self, symbols: set[str]) -> None:
        self.symbols = set(symbols)


@dataclass
class StrategyContext:
    alpha_id: str
    version: str
    cache: SharedCandleCache
    signal_dispatcher: object | None
    state: StrategyRuntimeState
    warmup_min_symbol_coverage: float = 0.90
    price_alerts: PriceAlertProxy | None = None

    def can_open_trades(self) -> bool:
        return self.state.can_open_new_trades()

    def update_readiness(
        self,
        symbols: list[str],
        tf: str,
        bars: int,
        max_age_sec: float | None = None,
    ) -> bool:
        loaded, _total, pct = self.cache.coverage(symbols, tf, bars, max_age_sec)
        required = max(1, int(len(symbols) * self.warmup_min_symbol_coverage + 0.999999))
        self.state.ready = loaded >= required
        return self.state.ready

    async def emit_signal(self, signal_type: str, **fields):
        if self.signal_dispatcher is None:
            return None
        return await self.signal_dispatcher.dispatch(self, signal_type, **fields)


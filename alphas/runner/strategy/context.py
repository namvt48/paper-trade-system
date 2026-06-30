from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from runner.data_layer.cache import SharedCandleCache
from runner.reconcile.state import StrategyRuntimeState
from runner.shared_panel_feature_cache import SharedPanelFeatureCache

logger = logging.getLogger(__name__)

POSITIONS_KEY_PREFIX = "runner:positions:"


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
    _excluded_symbols: set[str] | None = None
    redis_client: object | None = None
    panel_feature_cache: SharedPanelFeatureCache | None = None

    @property
    def excluded_symbols(self) -> set[str]:
        return self._excluded_symbols or set()

    @excluded_symbols.setter
    def excluded_symbols(self, value: set[str]) -> None:
        self._excluded_symbols = value

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

    def save_positions(self, positions: dict) -> None:
        if self.redis_client is None or not positions:
            return
        key = f"{POSITIONS_KEY_PREFIX}{self.alpha_id}"
        try:
            self.redis_client.set(key, json.dumps(positions))
        except Exception as exc:
            logger.warning(
                "[CTX] Failed to persist positions for %s: %s",
                self.alpha_id,
                exc,
                extra={"alpha_id": self.alpha_id},
            )

    def load_positions(self) -> dict:
        if self.redis_client is None:
            return {}
        key = f"{POSITIONS_KEY_PREFIX}{self.alpha_id}"
        try:
            raw = self.redis_client.get(key)
            if raw:
                return json.loads(raw)
        except Exception as exc:
            logger.warning(
                "[CTX] Failed to load positions for %s: %s",
                self.alpha_id,
                exc,
                extra={"alpha_id": self.alpha_id},
            )
        return {}

    def clear_positions(self) -> None:
        if self.redis_client is None:
            return
        key = f"{POSITIONS_KEY_PREFIX}{self.alpha_id}"
        try:
            self.redis_client.delete(key)
        except Exception as exc:
            logger.warning(
                "[CTX] Failed to clear positions for %s: %s",
                self.alpha_id,
                exc,
                extra={"alpha_id": self.alpha_id},
            )

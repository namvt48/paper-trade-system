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
    mds_redis_client: object | None = None
    panel_feature_cache: SharedPanelFeatureCache | None = None
    # MDS's live tradable universe (quoteAsset=USDT, contractType=PERPETUAL/
    # TRADIFI_PERPETUAL, status=TRADING), pushed via the `symbols:{exchange}`
    # broadcast. None means "not received yet" -- callers must fail open
    # (don't block) rather than treat an empty/unknown set as "nothing tradable".
    live_tradable_symbols: set[str] | None = None

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

    def load_authoritative_positions(self) -> dict | None:
        """Read the worker's authoritative open-position snapshot.

        The worker DB (published to ``paper:positions:snapshot:{alpha_id}``) -- not the
        runner-side ``runner:positions`` cache -- is the source of truth for *which*
        positions are open: it is what enforces ``DUPLICATE_POSITION_POLICY``. A runner
        that trusts only its own cache can silently diverge from the DB across restarts,
        orphaning a DB position it no longer manages (never closed) while every new OPEN
        on that symbol is rejected.

        Returns ``{position_id: normalized_position}`` when the snapshot is present
        (possibly empty), or ``None`` when it is absent/unreadable -- callers MUST treat
        ``None`` ("worker view unknown") differently from ``{}`` ("DB has none open").
        """
        if self.redis_client is None:
            return None
        try:
            raw = self.redis_client.get(f"paper:positions:snapshot:{self.alpha_id}")
        except Exception as exc:
            logger.warning(
                "[CTX] Failed to read authoritative snapshot for %s: %s",
                self.alpha_id,
                exc,
                extra={"alpha_id": self.alpha_id},
            )
            return None
        from base.position_reconcile import normalize_position, parse_snapshot

        snapshot = parse_snapshot(raw)
        if snapshot is None:
            return None
        result: dict = {}
        for raw_pos in snapshot.get("positions", []):
            if not isinstance(raw_pos, dict) or not raw_pos.get("position_id"):
                continue
            try:
                normalized = normalize_position(raw_pos)
            except Exception as exc:
                logger.warning(
                    "[CTX] Skipping unparseable authoritative position for %s: %s",
                    self.alpha_id,
                    exc,
                    extra={"alpha_id": self.alpha_id},
                )
                continue
            result[str(normalized["position_id"])] = normalized
        return result

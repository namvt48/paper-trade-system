from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Callable

from base import signal_push
from portfolio_manager.core.engine import PortfolioEngine

logger = logging.getLogger(__name__)

_WEIGHT_TOLERANCE = 1e-9


def _read_snapshot(redis_client: Any, alpha_id: str) -> list[dict[str, Any]]:
    raw = redis_client.get(f"paper:positions:snapshot:{alpha_id}")
    if raw is None:
        return []
    parsed = json.loads(raw)
    if isinstance(parsed, dict):
        positions = parsed.get("positions", [])
    else:
        positions = parsed
    return [dict(position) for position in positions if isinstance(position, dict)]


def _position_weight(position: Mapping[str, Any]) -> float | None:
    raw = position.get("weight")
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    try:
        metadata = json.loads(str(position.get("metadata", "{}")))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if isinstance(metadata, Mapping) and metadata.get("weight") is not None:
        try:
            return float(str(metadata["weight"]))
        except (TypeError, ValueError):
            return None
    return None


class PortfolioService:
    """Operational adapter around the pure PM cycle engine."""

    def __init__(
        self,
        config: dict[str, Any],
        redis_client: Any,
        *,
        publish: Callable[..., None] | None = None,
    ) -> None:
        self.config = config
        self.redis = redis_client
        self.engine = PortfolioEngine(config, redis_client)
        self.alpha_id = str(config["alpha_id"])
        self.capital = float(config["capital"])
        self.publish = publish or self._publish_signal

    def _publish_signal(self, signal_type: str, **fields: Any) -> None:
        signal_push.push_signal(signal_type, self.alpha_id, **fields)

    def run_cycle(
        self,
        *,
        regime_state: Mapping[str, float | bool] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        result = self.engine.cycle(regime_state=dict(regime_state or {}), now=now)
        # Two independent gates (R13): regime's flag only decides whether the
        # throttled candidate is used instead of baseline; it must never also
        # decide whether PM executes at all, or a disabled regime would
        # silently mean "never trade" instead of "trade the untouched book".
        regime_execution_enabled = bool(
            self.config.get("regime", {}).get("execution_enabled", False)
        )
        pm_execution_enabled = bool(
            self.config.get("execution", {}).get("enabled", False)
        )
        selected = result.candidate if regime_execution_enabled else result.baseline
        prices = self._prices_from_books()
        published = 0
        if pm_execution_enabled:
            published = self._reconcile_and_publish(selected, prices)
        else:
            logger.info("PM execution disabled; cycle remains shadow-only")
        return {
            "active_sleeves": result.active_sleeves,
            "stale_sleeves": result.stale_sleeves,
            "baseline": result.baseline,
            "candidate": result.candidate,
            "selected": selected,
            "published": published,
            "execution_enabled": pm_execution_enabled,
            "regime_execution_enabled": regime_execution_enabled,
        }

    def _prices_from_books(self) -> dict[str, float]:
        prices: dict[str, float] = {}
        for sleeve in self.config["sleeves"]:
            book = self.engine.store.read(str(sleeve["id"]))
            if book is None:
                continue
            raw_prices = book.meta.get("prices", {})
            if isinstance(raw_prices, Mapping):
                for symbol, value in raw_prices.items():
                    try:
                        price = float(str(value))
                    except (TypeError, ValueError):
                        continue
                    if price > 0:
                        prices.setdefault(str(symbol), price)
        return prices

    def _reconcile_and_publish(
        self, target: Mapping[str, float], prices: Mapping[str, float]
    ) -> int:
        current = _read_snapshot(self.redis, self.alpha_id)
        current_by_symbol = {
            str(pos.get("symbol")): pos for pos in current if pos.get("symbol")
        }
        published = 0
        closed_symbols: set[str] = set()
        for symbol, position in current_by_symbol.items():
            target_weight = float(target.get(symbol, 0.0))
            target_side = (
                "LONG"
                if target_weight > 0
                else "SHORT"
                if target_weight < 0
                else "FLAT"
            )
            current_side = str(position.get("side", ""))
            current_weight = _position_weight(position)
            # A same-side resize must ALSO close first: the worker rejects a
            # second OPEN on the same (alpha_id, symbol) while one is still
            # open (executor.py duplicate_policy="reject"), so publishing
            # OPEN alone here would leave the position stuck at its old
            # weight forever instead of tracking the new target.
            resized = (
                target_side == current_side
                and current_weight is not None
                and abs(current_weight - target_weight) > _WEIGHT_TOLERANCE
            )
            if target_side != current_side or resized:
                self.publish(
                    "CLOSE",
                    symbol=symbol,
                    position_id=position.get("position_id"),
                    reason="PM_RECONCILE",
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
                published += 1
                closed_symbols.add(symbol)

        for symbol, weight in target.items():
            if not weight:
                continue
            current = current_by_symbol.get(symbol)
            unchanged = (
                current is not None
                and symbol not in closed_symbols
                and str(current.get("side")) == ("LONG" if weight > 0 else "SHORT")
            )
            if unchanged:
                continue
            price = float(prices.get(symbol, 0.0))
            if price <= 0:
                logger.warning(
                    "[%s] cannot publish %s: no sleeve price", self.alpha_id, symbol
                )
                continue
            position_id = str(uuid.uuid4())
            self.publish(
                "OPEN",
                symbol=symbol,
                side="LONG" if weight > 0 else "SHORT",
                entry=price,
                qty=self.capital * abs(float(weight)) / price,
                leverage=1,
                position_id=position_id,
                exchange=str(self.config.get("exchange", "binance")),
                fee_pct=float(self.config.get("fee_bps", 7.0)) / 10_000,
                metadata=json.dumps(
                    {"weight": float(weight), "pm_alpha_id": self.alpha_id}
                ),
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            published += 1
        return published

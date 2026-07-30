from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_VIRTUAL_TRADE_STREAM = "paper-shadow-trades"
_STATE_KEY_PREFIX = "shadow:ledger:positions:"
_ID_NAMESPACE = uuid.UUID("8a5b1ea4-fec4-4e3f-a434-165bea2b2a18")


def _stable_id(*parts: object) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, ":".join(str(part) for part in parts)))


class VirtualTradeLedgerPublisher:
    """Publish a shadow strategy's rebalance lifecycle to an isolated stream.

    This publisher never receives or references the real execution stream.
    Its Redis state is only the restart-safe set of virtual positions needed
    to turn the next rebalance into deterministic close events.
    """

    def __init__(
        self,
        redis_client: Any,
        alpha_id: str,
        capital: float,
        exchange: str,
        stream: str = DEFAULT_VIRTUAL_TRADE_STREAM,
    ) -> None:
        self._redis = redis_client
        self.alpha_id = alpha_id
        self.capital = float(capital)
        self.exchange = exchange
        self.stream = stream
        self.state_key = f"{_STATE_KEY_PREFIX}{alpha_id}"
        self._positions, state_exists = self._load_positions()
        if not state_exists:
            self._bootstrap_from_target_book()

    def _load_positions(self) -> tuple[dict[str, dict[str, Any]], bool]:
        try:
            raw = self._redis.get(self.state_key)
            if raw is None:
                return {}, False
            parsed = json.loads(raw) if raw else {}
            if isinstance(parsed, dict):
                return (
                    {
                        str(symbol): position
                        for symbol, position in parsed.items()
                        if isinstance(position, dict)
                    },
                    True,
                )
        except Exception as exc:
            logger.warning(
                "[VIRTUAL-LEDGER] failed to restore alpha=%s: %s",
                self.alpha_id,
                exc,
                extra={"alpha_id": self.alpha_id},
            )
        return {}, False

    def _bootstrap_from_target_book(self) -> None:
        """Seed the currently active sleeve book after the ledger is introduced."""
        try:
            raw = self._redis.get(f"book:target:{self.alpha_id}")
            if not raw:
                return
            book = json.loads(raw)
            weights = book.get("weights")
            meta = book.get("meta") or {}
            prices = meta.get("prices")
            if not isinstance(weights, dict) or not isinstance(prices, dict):
                return
            diagnostics = meta.get("diagnostics")
            common_metadata = (
                diagnostics if isinstance(diagnostics, dict) else {}
            )
            self.rebalance(
                weights={str(symbol): float(weight) for symbol, weight in weights.items()},
                prices={str(symbol): float(price) for symbol, price in prices.items()},
                candle_open_ms=int(book["as_of_candle_ms"]),
                timeframe=str(book["timeframe"]),
                metadata_by_symbol={
                    str(symbol): {
                        **common_metadata,
                        "bootstrap_source": "target_book",
                    }
                    for symbol in weights
                },
                event_at=str(book["generated_at"]),
            )
        except Exception:
            logger.exception(
                "[VIRTUAL-LEDGER] target-book bootstrap failed alpha=%s",
                self.alpha_id,
                extra={"alpha_id": self.alpha_id},
            )

    def rebalance(
        self,
        *,
        weights: dict[str, float],
        prices: dict[str, float],
        candle_open_ms: int,
        timeframe: str,
        metadata_by_symbol: dict[str, dict[str, Any]],
        event_at: str | None = None,
        close_reason: str = "VIRTUAL_REBALANCE",
    ) -> None:
        event_at = event_at or datetime.now(timezone.utc).isoformat()
        events: list[dict[str, Any]] = []
        next_positions: dict[str, dict[str, Any]] = {}

        for symbol, position in sorted(self._positions.items()):
            exit_price = float(prices.get(symbol, 0.0))
            if exit_price <= 0:
                next_positions[symbol] = position
                logger.warning(
                    "[VIRTUAL-LEDGER] retaining %s/%s because rebalance price is missing",
                    self.alpha_id,
                    symbol,
                    extra={"alpha_id": self.alpha_id},
                )
                continue
            events.append(
                {
                    "ledger_mode": "virtual",
                    "type": "VIRTUAL_CLOSE",
                    "event_id": _stable_id(
                        "close", position["position_id"], candle_open_ms
                    ),
                    "position_id": position["position_id"],
                    "alpha_id": self.alpha_id,
                    "symbol": symbol,
                    "side": position["side"],
                    "price": exit_price,
                    "qty": float(position["qty"]),
                    "weight": float(position["weight"]),
                    "exchange": self.exchange,
                    "timeframe": timeframe,
                    "timestamp": event_at,
                    "candle_open_ms": int(candle_open_ms),
                    "reason": close_reason,
                    "metadata": {"virtual": True, "ledger_source": "shadow_sleeve"},
                }
            )

        for symbol, raw_weight in sorted(weights.items()):
            weight = float(raw_weight)
            entry_price = float(prices.get(symbol, 0.0))
            if weight == 0 or entry_price <= 0 or symbol in next_positions:
                continue
            position_id = _stable_id(
                "position", self.alpha_id, symbol, candle_open_ms
            )
            metadata = {
                **metadata_by_symbol.get(symbol, {}),
                "virtual": True,
                "ledger_source": "shadow_sleeve",
                "weight": weight,
            }
            position = {
                "position_id": position_id,
                "alpha_id": self.alpha_id,
                "symbol": symbol,
                "side": "LONG" if weight > 0 else "SHORT",
                "entry_price": entry_price,
                "qty": self.capital * abs(weight) / entry_price,
                "weight": weight,
                "exchange": self.exchange,
                "timeframe": timeframe,
                "opened_at": event_at,
                "entry_candle_open_ms": int(candle_open_ms),
                "metadata": metadata,
            }
            next_positions[symbol] = position
            events.append(
                {
                    "ledger_mode": "virtual",
                    "type": "VIRTUAL_OPEN",
                    "event_id": _stable_id("open", position_id),
                    "position_id": position_id,
                    "alpha_id": self.alpha_id,
                    "symbol": symbol,
                    "side": position["side"],
                    "price": entry_price,
                    "qty": position["qty"],
                    "weight": weight,
                    "exchange": self.exchange,
                    "timeframe": timeframe,
                    "timestamp": event_at,
                    "candle_open_ms": int(candle_open_ms),
                    "metadata": metadata,
                }
            )

        pipeline = self._redis.pipeline(transaction=True)
        for event in events:
            pipeline.xadd(
                self.stream,
                {"payload": json.dumps(event, separators=(",", ":"), sort_keys=True)},
            )
        pipeline.set(
            self.state_key,
            json.dumps(next_positions, separators=(",", ":"), sort_keys=True),
        )
        pipeline.execute()
        self._positions = next_positions
        logger.info(
            "[VIRTUAL-LEDGER] alpha=%s events=%d open=%d stream=%s",
            self.alpha_id,
            len(events),
            len(next_positions),
            self.stream,
            extra={"alpha_id": self.alpha_id},
        )

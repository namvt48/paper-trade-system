from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ReadySignal:
    tf: str
    exchange: str
    timestamp: int
    complete_count: int
    partial_count: int
    insufficient_count: int
    partial_symbols: dict[str, float] = field(default_factory=dict)
    insufficient_symbols: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> ReadySignal:
        return cls(
            tf=data.get("tf", ""),
            exchange=data.get("exchange", ""),
            timestamp=data.get("timestamp", 0),
            complete_count=data.get("complete_count", 0),
            partial_count=data.get("partial_count", 0),
            insufficient_count=data.get("insufficient_count", 0),
            partial_symbols=data.get("partial_symbols", {}),
            insufficient_symbols=data.get("insufficient_symbols", []),
        )


class MDSReadyWatcher:
    def __init__(self, redis_client, exchange: str = "binance") -> None:
        self._redis = redis_client
        self._exchange = exchange
        self._signals: dict[str, ReadySignal] = {}

    @property
    def signals(self) -> dict[str, ReadySignal]:
        return dict(self._signals)

    async def wait_for_ready(
        self,
        required_tfs: list[str],
        timeout_sec: float = 900.0,
    ) -> dict[str, ReadySignal]:
        deadline = time.monotonic() + timeout_sec
        remaining = set(required_tfs)

        for tf in list(remaining):
            key = f"mds:warmup:ready:{self._exchange}:{tf}"
            data = self._redis_get(key)
            if data:
                self._signals[tf] = ReadySignal.from_dict(json.loads(data))
                remaining.discard(tf)

        if not remaining:
            return dict(self._signals)

        poll_interval = 2.0
        max_poll_interval = 15.0
        attempt = 0
        while remaining and time.monotonic() < deadline:
            await asyncio.sleep(poll_interval)
            attempt += 1
            for tf in list(remaining):
                key = f"mds:warmup:ready:{self._exchange}:{tf}"
                data = self._redis_get(key)
                if data:
                    self._signals[tf] = ReadySignal.from_dict(json.loads(data))
                    remaining.discard(tf)
                    logger.info("[MDS-READY] Received ready signal for %s", tf)
            if attempt % 10 == 0 and remaining:
                elapsed = timeout_sec - (deadline - time.monotonic())
                logger.info(
                    "[MDS-READY] Still waiting for TFs: %s (%.0fs elapsed)",
                    ", ".join(sorted(remaining)), elapsed,
                )
            poll_interval = min(poll_interval * 1.2, max_poll_interval)

        if remaining:
            logger.error(
                "[MDS-READY] Timeout waiting for TFs: %s",
                ", ".join(sorted(remaining)),
            )

        return dict(self._signals)

    def _redis_get(self, key: str) -> str | None:
        try:
            return self._redis.get(key)
        except Exception:
            return None

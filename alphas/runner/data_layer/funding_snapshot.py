from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class FundingSnapshotReader:
    """Reads MDS's per-symbol funding-rate snapshot list.

    Mirrors ``runner.data_layer.snapshot.SnapshotReader``'s read-decode-dedupe
    pattern, but for the single-field, low-frequency funding feed: no
    staleness check and no legacy-key fallback, since funding only refreshes
    every few minutes on the MDS side (unlike klines, which need a fresh
    check every candle).
    """

    def __init__(self, redis_client, exchange: str) -> None:
        self.redis = redis_client
        self.exchange = exchange

    def load(self, symbol: str, rows: int = 500) -> list[dict] | None:
        rows = int(rows)
        if rows <= 0:
            return []
        try:
            values = self.redis.lrange(self._key(symbol), 0, rows - 1)
        except Exception as exc:
            logger.warning("[RUNNER-FUNDING] read failed %s: %s", symbol, exc)
            return None
        if not values:
            return None
        return self._decode_sort(values, symbol)

    def load_many(self, symbols: list[str], rows: int = 500) -> dict[str, list[dict] | None]:
        """Batch equivalent of ``load`` for a whole universe: one pipelined
        round-trip instead of one blocking ``lrange`` per symbol. A
        cross-sectional universe of ~180 symbols previously meant ~180
        sequential synchronous calls per scan -- each one able to stall the
        calling thread if that single round-trip is slow."""
        rows = int(rows)
        if not symbols:
            return {}
        if rows <= 0:
            return {symbol: [] for symbol in symbols}
        pipe = self.redis.pipeline(transaction=False)
        for symbol in symbols:
            pipe.lrange(self._key(symbol), 0, rows - 1)
        try:
            results = pipe.execute()
        except Exception as exc:
            logger.warning("[RUNNER-FUNDING] batch read failed (%d symbols): %s", len(symbols), exc)
            return {symbol: None for symbol in symbols}
        return {
            symbol: (self._decode_sort(values, symbol) if values else None)
            for symbol, values in zip(symbols, results)
        }

    def _decode_sort(self, values: list[Any], symbol: str) -> list[dict] | None:
        by_time: dict[int, dict] = {}
        for raw in values:
            try:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                row = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                logger.warning("[RUNNER-FUNDING] decode failed %s: %s", symbol, exc)
                return None
            if not isinstance(row, dict):
                continue
            try:
                funding_time = int(row["funding_time"])
            except (KeyError, TypeError, ValueError):
                continue
            by_time[funding_time] = row
        if not by_time:
            return None
        return [by_time[t] for t in sorted(by_time)]

    def _key(self, symbol: str) -> str:
        return f"funding_snapshot:{self.exchange}:{symbol}"

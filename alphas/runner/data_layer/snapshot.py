from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


def _tf_seconds(tf: str) -> int:
    value = str(tf).strip().lower()
    if not value:
        return 60
    unit = value[-1]
    try:
        amount = int(value[:-1])
    except ValueError:
        return 60
    if unit == "m":
        return amount * 60
    if unit == "h":
        return amount * 60 * 60
    if unit == "d":
        return amount * 24 * 60 * 60
    if unit == "w":
        return amount * 7 * 24 * 60 * 60
    return max(amount, 1)


class SnapshotReader:
    def __init__(
        self,
        redis_client,
        exchange: str,
        max_stale_sec_by_tf: dict[str, float] | None = None,
        now_func: Callable[[], float] | None = None,
    ) -> None:
        self.redis = redis_client
        self.exchange = exchange
        self.max_stale_sec_by_tf = dict(max_stale_sec_by_tf or {})
        self._now = now_func or time.time

    def load(self, symbol: str, tf: str, bars: int) -> list[dict] | None:
        bars = int(bars)
        if bars <= 0:
            return []
        try:
            values = self.redis.lrange(self._v2_key(symbol, tf), 0, bars - 1)
        except Exception as exc:
            logger.warning("[RUNNER-SNAPSHOT] v2 read failed %s %s: %s", symbol, tf, exc)
            return None

        if values:
            return self._decode_validate(values, symbol, tf, bars)

        try:
            raw = self.redis.hgetall(self._legacy_key(symbol, tf))
        except Exception as exc:
            logger.warning("[RUNNER-SNAPSHOT] legacy read failed %s %s: %s", symbol, tf, exc)
            return None
        legacy_values = list((raw or {}).values())
        if not legacy_values:
            return None
        return self._decode_validate(legacy_values, symbol, tf, bars)

    def _decode_validate(
        self,
        values: list[Any],
        symbol: str,
        tf: str,
        bars: int,
    ) -> list[dict] | None:
        candles: list[dict] = []
        for raw in values:
            try:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                candle = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                logger.warning("[RUNNER-SNAPSHOT] decode failed %s %s: %s", symbol, tf, exc)
                return None
            if isinstance(candle, dict):
                candles.append(candle)

        candles = self._dedupe_sort(candles)
        if len(candles) < bars:
            return None
        candles = candles[-bars:]
        if self._is_stale(candles[-1], tf):
            return None
        return candles

    def _is_stale(self, candle: dict, tf: str) -> bool:
        try:
            latest_ms = int(candle.get("open_time", candle.get("time", 0)))
        except (TypeError, ValueError):
            return True
        max_stale = self.max_stale_sec_by_tf.get(tf, max(3 * _tf_seconds(tf), 600))
        return self._now() - (latest_ms / 1000.0) > max_stale

    @staticmethod
    def _dedupe_sort(candles: list[dict]) -> list[dict]:
        by_open_time: dict[int, dict] = {}
        for candle in candles:
            try:
                open_time = int(candle.get("open_time", candle.get("time", 0)))
            except (TypeError, ValueError):
                continue
            if open_time > 0:
                by_open_time[open_time] = candle
        return [by_open_time[t] for t in sorted(by_open_time)]

    def _v2_key(self, symbol: str, tf: str) -> str:
        return f"kline_snapshot_v2:{self.exchange}:{tf}:{symbol}"

    def _legacy_key(self, symbol: str, tf: str) -> str:
        return f"kline_snapshot:{self.exchange}:{tf}:{symbol}"

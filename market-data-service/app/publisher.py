from __future__ import annotations

import json
import logging

import redis as redis_lib

from app.models import KlineCandle, TickerUpdate

logger = logging.getLogger(__name__)


class Publisher:
    def __init__(self, redis_client: redis_lib.Redis, snapshot_max_candles: int = 500):
        self._redis = redis_client
        self._snapshot_max_candles = snapshot_max_candles

    def publish_kline(self, candle: KlineCandle) -> None:
        payload = json.dumps(candle.to_dict())
        channel = f"kline:{candle.tf}"
        self._redis.publish(channel, payload)
        self.publish_kline_snapshot(candle, max_candles=self._snapshot_max_candles)
        if candle.tf != "1m":
            logger.debug("[PUB] %s %s correction=%s", channel, candle.symbol, candle.correction)

    def publish_ticker(self, ticker: TickerUpdate) -> None:
        self._redis.publish("ticker", json.dumps(ticker.to_dict()))

    def publish_symbols(self, symbols: list[str]) -> None:
        self._redis.publish("symbols", json.dumps({"symbols": symbols}))

    def publish_kline_snapshot(self, candle: KlineCandle, max_candles: int = 500) -> None:
        key = f"kline_snapshot:{candle.tf}:{candle.symbol}"
        self._redis.hset(key, str(candle.open_time), json.dumps(candle.to_dict()))
        fields = self._redis.hkeys(key)
        if len(fields) <= max_candles:
            return

        oldest = sorted(fields, key=int)[: len(fields) - max_candles]
        if oldest:
            self._redis.hdel(key, *oldest)

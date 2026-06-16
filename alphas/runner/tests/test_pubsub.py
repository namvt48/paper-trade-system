from __future__ import annotations

import json
import asyncio

import pytest

from runner.data_layer.cache import SharedCandleCache
from runner.data_layer.pubsub import SharedPubSubManager


class FakePubSub:
    def __init__(self):
        self.subscribed = []
        self.unsubscribed = []
        self.messages = []

    def subscribe(self, channel):
        self.subscribed.append(channel)

    def unsubscribe(self, channel):
        self.unsubscribed.append(channel)

    def get_message(self, ignore_subscribe_messages=True, timeout=1.0):
        if self.messages:
            return self.messages.pop(0)
        return None


class FakeRedis:
    _runner_inline_redis = True

    def __init__(self):
        self.ps = FakePubSub()

    def pubsub(self):
        return self.ps


@pytest.mark.asyncio
async def test_pubsub_subscribes_once_for_shared_channel_and_unsubscribes_last():
    redis = FakeRedis()
    manager = SharedPubSubManager(redis, SharedCandleCache())

    await manager.subscribe("kline:binance:15m", "a1")
    await manager.subscribe("kline:binance:15m", "a2")
    await manager.unsubscribe("kline:binance:15m", "a1")
    await manager.unsubscribe("kline:binance:15m", "a2")

    assert redis.ps.subscribed == ["kline:binance:15m"]
    assert redis.ps.unsubscribed == ["kline:binance:15m"]


@pytest.mark.asyncio
async def test_pubsub_updates_cache_even_when_queue_full_and_counts_drop():
    manager = SharedPubSubManager(FakeRedis(), SharedCandleCache(), queue_maxsize=1)
    q = await manager.subscribe("kline:binance:15m", "a1")
    q.put_nowait(object())

    await manager.handle_message("kline:binance:15m", json.dumps({
        "symbol": "BTCUSDT", "tf": "15m", "open_time": 1000,
        "open": 1, "high": 2, "low": 0, "close": 1.5, "volume": 10,
    }))

    assert manager.cache.get_closes("BTCUSDT", "15m") == (1.5,)
    assert manager.stats()["dropped_events"]["a1"] == 1


@pytest.mark.asyncio
async def test_pubsub_run_pumps_messages_into_strategy_queue_and_cache():
    redis = FakeRedis()
    manager = SharedPubSubManager(redis, SharedCandleCache())
    q = await manager.subscribe("kline:binance:15m", "a1")
    redis.ps.messages.append({
        "type": "message",
        "channel": "kline:binance:15m",
        "data": json.dumps({
            "symbol": "BTCUSDT", "tf": "15m", "open_time": 1000,
            "open": 1, "high": 2, "low": 0, "close": 1.5, "volume": 10,
        }),
    })
    stop = asyncio.Event()

    task = asyncio.create_task(manager.run(stop, poll_timeout=0.001))
    event = await asyncio.wait_for(q.get(), timeout=1.0)
    stop.set()
    await task

    assert event.kind == "kline"
    assert event.symbol == "BTCUSDT"
    assert manager.cache.get_closes("BTCUSDT", "15m") == (1.5,)

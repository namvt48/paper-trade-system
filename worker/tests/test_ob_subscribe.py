import json

import fakeredis.aioredis
import pytest

from app.ob_subscribe import publish_subscribe, publish_sync


@pytest.mark.asyncio
async def test_publish_sync_payload():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    pubsub = r.pubsub()
    await pubsub.subscribe("orderbook:subscribe:binance")
    await pubsub.get_message(timeout=1)  # drain ack

    await publish_sync(r, "binance", "worker-1", ["BTCUSDT", "ETHUSDT"])

    msg = await pubsub.get_message(timeout=1)
    payload = json.loads(msg["data"])
    assert payload["action"] == "sync"
    assert payload["consumer_id"] == "worker-1"
    assert sorted(payload["symbols"]) == ["BTCUSDT", "ETHUSDT"]


@pytest.mark.asyncio
async def test_publish_subscribe_single_symbol():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    pubsub = r.pubsub()
    await pubsub.subscribe("orderbook:subscribe:binance")
    await pubsub.get_message(timeout=1)

    await publish_subscribe(r, "binance", "worker-1", "SOLUSDT")

    msg = await pubsub.get_message(timeout=1)
    payload = json.loads(msg["data"])
    assert payload["action"] == "subscribe"
    assert payload["symbols"] == ["SOLUSDT"]

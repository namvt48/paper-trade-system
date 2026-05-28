import json
import os

import pytest
import redis as redis_lib

from app.models import KlineCandle, TickerUpdate
from app.publisher import Publisher


@pytest.fixture
def redis_client():
    if os.getenv("RUN_REDIS_TESTS") != "1":
        pytest.skip("Set RUN_REDIS_TESTS=1 to run Redis Pub/Sub tests")
    client = redis_lib.Redis(decode_responses=True, socket_connect_timeout=0.2, socket_timeout=0.2)
    try:
        client.ping()
    except redis_lib.RedisError:
        pytest.skip("Redis is not available")
    yield client
    client.close()


def test_publish_kline(redis_client):
    publisher = Publisher(redis_client)
    pubsub = redis_client.pubsub()
    pubsub.subscribe("kline:1m")
    pubsub.get_message(timeout=1.0)

    publisher.publish_kline(
        KlineCandle(
            symbol="BTCUSDT",
            tf="1m",
            open=67000.0,
            high=67500.0,
            low=66800.0,
            close=67200.0,
            volume=12345.6,
            open_time=1716768000000,
            close_time=1716771599999,
        )
    )

    msg = pubsub.get_message(timeout=1.0)
    pubsub.unsubscribe()
    pubsub.close()

    assert msg is not None
    data = json.loads(msg["data"])
    assert data["symbol"] == "BTCUSDT"
    assert data["tf"] == "1m"
    assert data["close"] == 67200.0


def test_publish_ticker(redis_client):
    publisher = Publisher(redis_client)
    pubsub = redis_client.pubsub()
    pubsub.subscribe("ticker")
    pubsub.get_message(timeout=1.0)

    publisher.publish_ticker(
        TickerUpdate(
            symbol="ETHUSDT",
            price=3000.5,
            timestamp=1716771600000,
            exchange="binance",
        )
    )

    msg = pubsub.get_message(timeout=1.0)
    pubsub.unsubscribe()
    pubsub.close()

    assert msg is not None
    data = json.loads(msg["data"])
    assert data["symbol"] == "ETHUSDT"
    assert data["price"] == 3000.5

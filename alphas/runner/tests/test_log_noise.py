from __future__ import annotations

import asyncio
import logging
from unittest.mock import Mock, patch

import pytest

from runner.data_layer.cache import SharedCandleCache
from runner.data_layer.pubsub import SharedPubSubManager
from runner.main import periodic_gap_check


class StrategyStub:
    def get_warmup_tfs(self):
        return ["1m"]


def candle(open_time: int) -> dict:
    return {
        "open_time": open_time,
        "open": 1,
        "high": 2,
        "low": 0,
        "close": 1,
        "volume": 1,
    }


@pytest.mark.asyncio
async def test_pubsub_received_message_is_debug(caplog):
    manager = SharedPubSubManager(Mock(), SharedCandleCache())
    messages = [
        {
            "type": "message",
            "channel": "kline:binance:1h",
            "data": {
                "symbol": "BTCUSDT",
                "tf": "1h",
                "open_time": 3_600_000,
            },
        },
        None,
    ]

    async def get_message(_poll_timeout):
        return messages.pop(0)

    manager._get_message = get_message
    stop_event = asyncio.Event()

    async def stop_after_first_message(*_args, **_kwargs):
        stop_event.set()

    manager.handle_message = stop_after_first_message

    with caplog.at_level(logging.INFO, logger="runner.data_layer.pubsub"):
        await manager.run(stop_event, poll_timeout=0.01)

    assert "Received message on channel=kline:binance:1h" not in caplog.text


@pytest.mark.asyncio
async def test_periodic_gap_check_warns_once_for_repeated_gap_signature():
    cache = SharedCandleCache()
    cache.upsert_candle("BTCUSDT", "1m", candle(60_000))
    cache.upsert_candle("BTCUSDT", "1m", candle(180_000))
    stop_event = asyncio.Event()

    async def wait_with_one_repeat(awaitable, timeout=None):
        awaitable.close()
        if wait_with_one_repeat.calls == 0:
            wait_with_one_repeat.calls += 1
            raise asyncio.TimeoutError
        stop_event.set()
        raise asyncio.TimeoutError

    wait_with_one_repeat.calls = 0

    with patch("runner.main.asyncio.wait_for", side_effect=wait_with_one_repeat):
        with patch("runner.main.logger") as mock_logger:
            await periodic_gap_check(
                cache,
                [StrategyStub()],
                interval_sec=300.0,
                stop_event=stop_event,
            )

    assert mock_logger.warning.call_count == 1
    assert mock_logger.debug.call_count == 1

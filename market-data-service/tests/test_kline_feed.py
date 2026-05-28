import pytest
from unittest.mock import AsyncMock

from app.aggregator import Aggregator
from app.kline_feed import KlineFeed


@pytest.fixture
def feed():
    return KlineFeed(aggregator=Aggregator(timeframes=["1m", "5m", "15m", "1h"]), ws_batch_size=150)


def test_build_stream_names(feed):
    assert feed.build_stream_names(["BTCUSDT", "ETHUSDT"], "1m") == [
        "btcusdt@kline_1m",
        "ethusdt@kline_1m",
    ]


def test_batch_symbols(feed):
    symbols = [f"SYM{i}USDT" for i in range(350)]
    batches = feed.batch_symbols(symbols)
    assert len(batches) == 3
    assert len(batches[0]) == 150
    assert len(batches[1]) == 150
    assert len(batches[2]) == 50


@pytest.mark.asyncio
async def test_process_message_confirmed(feed):
    results = await feed.process_message(
        {
            "e": "kline",
            "s": "BTCUSDT",
            "k": {
                "t": 1716768000000,
                "T": 1716771599999,
                "o": "67000.0",
                "h": "67500.0",
                "l": "66800.0",
                "c": "67200.0",
                "v": "100.0",
                "x": True,
            },
        }
    )
    assert results is not None
    assert results[0].symbol == "BTCUSDT"
    assert results[0].tf == "1m"


@pytest.mark.asyncio
async def test_process_message_partial_returns_none(feed):
    results = await feed.process_message(
        {
            "e": "kline",
            "s": "BTCUSDT",
            "k": {
                "t": 1716768000000,
                "T": 1716771599999,
                "o": "67000.0",
                "h": "67500.0",
                "l": "66800.0",
                "c": "67200.0",
                "v": "100.0",
                "x": False,
            },
        }
    )
    assert results is None


@pytest.mark.asyncio
async def test_process_message_non_kline_returns_none(feed):
    assert await feed.process_message({"e": "other"}) is None


@pytest.mark.asyncio
async def test_load_initial_data_paginates_over_binance_limit(feed):
    first_chunk = [
        [1000 + i * 60000, "1", "2", "0.5", "1.5", "10", 1000 + i * 60000 + 59999]
        for i in range(1500)
    ]
    second_chunk = [
        [first_chunk[0][0] - (1500 - i) * 60000, "1", "2", "0.5", "1.5", "10", first_chunk[0][0] - (1500 - i) * 60000 + 59999]
        for i in range(500)
    ]
    client = AsyncMock()
    client.futures_klines = AsyncMock(side_effect=[first_chunk, second_chunk])

    rows = await feed._fetch_initial_1m_klines(client, "BTCUSDT", 2000)

    assert len(rows) == 2000
    assert client.futures_klines.await_count == 2
    second_call = client.futures_klines.await_args_list[1]
    assert second_call.kwargs["endTime"] == first_chunk[0][0] - 1

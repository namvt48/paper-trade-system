import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.warmup_handler import WarmupHandler


def _make_kline(open_time: int, close_time: int, close: float = 100.0) -> list:
    return [open_time, "99.0", "101.0", "98.0", str(close), "500.0", close_time, "0", 100, "0", "0", "0"]


def _make_handler(futures_klines_side_effect=None):
    redis_mock = MagicMock()
    redis_mock.xadd = MagicMock()

    client_mock = AsyncMock()
    if futures_klines_side_effect is not None:
        client_mock.futures_klines = AsyncMock(side_effect=futures_klines_side_effect)
    else:
        client_mock.futures_klines = AsyncMock(return_value=[])

    rate_limiter_mock = AsyncMock()
    rate_limiter_mock.acquire = AsyncMock()

    return WarmupHandler(redis_mock, client_mock, rate_limiter_mock), redis_mock, client_mock


@pytest.mark.asyncio
async def test_fetch_tf_candles_returns_closed_bars():
    now_ms = int(time.time() * 1000)
    past = now_ms - 120_000
    klines = [_make_kline(past, past + 59_999)]

    handler, _, _ = _make_handler(futures_klines_side_effect=[klines, []])
    result = await handler._fetch_tf_candles("BTCUSDT", "1m", 50)

    assert len(result) == 1
    assert result[0]["symbol"] == "BTCUSDT"
    assert result[0]["tf"] == "1m"
    assert result[0]["confirmed"] is True


@pytest.mark.asyncio
async def test_fetch_tf_candles_excludes_open_bar():
    now_ms = int(time.time() * 1000)
    past = now_ms - 120_000
    open_bar = _make_kline(now_ms - 30_000, now_ms + 30_000)
    closed_bar = _make_kline(past, past + 59_999)
    klines = [closed_bar, open_bar]

    handler, _, _ = _make_handler(futures_klines_side_effect=[klines, []])
    result = await handler._fetch_tf_candles("BTCUSDT", "1m", 50)

    assert len(result) == 1
    assert result[0]["close_time"] == past + 59_999


@pytest.mark.asyncio
async def test_fetch_tf_candles_gap_sync():
    now_ms = int(time.time() * 1000)
    tf_ms = 15 * 60 * 1000

    # Historical bars ending 2 TF intervals ago
    hist_close = now_ms - tf_ms * 2
    hist = [_make_kline(hist_close - tf_ms, hist_close)]

    # Gap bar
    gap_close = now_ms - tf_ms
    gap = [_make_kline(gap_close - tf_ms, gap_close)]

    handler, _, _ = _make_handler(futures_klines_side_effect=[hist, gap])
    result = await handler._fetch_tf_candles("BTCUSDT", "15m", 50)

    assert len(result) == 2
    assert result[1]["close_time"] == gap_close


@pytest.mark.asyncio
async def test_process_request_sends_response_per_symbol():
    now_ms = int(time.time() * 1000)
    past = now_ms - 120_000
    klines = [_make_kline(past, past + 59_999)]

    handler, redis_mock, _ = _make_handler(futures_klines_side_effect=[klines, []] * 10)
    request = {
        "alpha_id": "test-alpha",
        "tf": "15m",
        "bars": "50",
        "symbols": "BTCUSDT,ETHUSDT",
    }
    await handler._process_request(request)

    assert redis_mock.xadd.call_count == 2
    for call in redis_mock.xadd.call_args_list:
        stream = call[0][0]
        assert stream == "warmup:response:test-alpha"
        fields = call[0][1]
        assert fields["tf"] == "15m"
        assert json.loads(fields["candles"]) is not None


@pytest.mark.asyncio
async def test_process_request_empty_on_fetch_failure():
    handler, redis_mock, client_mock = _make_handler()
    client_mock.futures_klines = AsyncMock(side_effect=Exception("API down"))

    request = {
        "alpha_id": "test-alpha",
        "tf": "15m",
        "bars": "50",
        "symbols": "BTCUSDT",
    }
    await handler._process_request(request)

    assert redis_mock.xadd.call_count == 1
    fields = redis_mock.xadd.call_args[0][1]
    assert json.loads(fields["candles"]) == []


@pytest.mark.asyncio
async def test_process_request_skips_missing_fields():
    handler, redis_mock, _ = _make_handler()
    await handler._process_request({"alpha_id": "x", "tf": "15m"})
    redis_mock.xadd.assert_not_called()

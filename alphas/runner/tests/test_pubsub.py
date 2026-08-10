from __future__ import annotations

import json
import asyncio
import time

import pytest
from unittest.mock import MagicMock

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


def test_pubsub_initializes_last_message_time():
    manager = SharedPubSubManager(FakeRedis(), SharedCandleCache())
    assert hasattr(manager, '_last_message_time')
    assert isinstance(manager._last_message_time, float)


def test_pubsub_initializes_stale_check_interval():
    manager = SharedPubSubManager(FakeRedis(), SharedCandleCache())
    assert hasattr(manager, '_stale_check_interval')
    assert manager._stale_check_interval == 30.0


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


@pytest.mark.asyncio
async def test_run_triggers_on_reconnect_when_data_stale_on_empty_polls():
    redis = FakeRedis()
    cache = SharedCandleCache()
    cache.register_data_requirement("BTCUSDT", "1m", warmup_bars=5, retain_bars=10)

    manager = SharedPubSubManager(redis, cache)
    manager._stale_check_interval = 0.01

    warmup_mock = MagicMock()
    warmup_mock.get_required_tfs = MagicMock(return_value=["1m"])
    manager.set_reconnect_handler(warmup_mock, staleness_candles=1)

    stale_ts = int(time.time() * 1000) - 900_000
    cache.upsert_candle("BTCUSDT", "1m", {
        "open_time": stale_ts, "open": 1, "high": 2, "low": 0.5,
        "close": 1.5, "volume": 100, "confirmed": True,
    })

    reconnect_called = False

    async def mock_reconnect(warmup_manager=None, staleness_candles=None):
        nonlocal reconnect_called
        reconnect_called = True

    manager._on_reconnect = mock_reconnect

    stop = asyncio.Event()
    task = asyncio.create_task(manager.run(stop, poll_timeout=0.001))
    await asyncio.sleep(0.2)
    stop.set()
    await task

    assert reconnect_called is True


@pytest.mark.asyncio
async def test_is_data_stale_returns_true_when_data_old():
    cache = SharedCandleCache()
    cache.register_data_requirement("BTCUSDT", "15m", warmup_bars=5, retain_bars=10)

    redis_mock = MagicMock()
    warmup_mock = MagicMock()
    warmup_mock.get_required_tfs = MagicMock(return_value=["15m"])

    manager = SharedPubSubManager(redis_mock, cache)

    stale_ts = int(time.time() * 1000) - 900_000 * 10
    cache.upsert_candle("BTCUSDT", "15m", {
        "open_time": stale_ts, "open": 1, "high": 2, "low": 0.5,
        "close": 1.5, "volume": 100, "confirmed": True,
    })

    assert manager._is_data_stale(warmup_mock, staleness_candles=5) is True


@pytest.mark.asyncio
async def test_is_data_stale_returns_false_when_data_fresh():
    cache = SharedCandleCache()
    cache.register_data_requirement("BTCUSDT", "15m", warmup_bars=5, retain_bars=10)

    redis_mock = MagicMock()
    warmup_mock = MagicMock()
    warmup_mock.get_required_tfs = MagicMock(return_value=["15m"])

    manager = SharedPubSubManager(redis_mock, cache)

    fresh_ts = int(time.time() * 1000) - 100
    cache.upsert_candle("BTCUSDT", "15m", {
        "open_time": fresh_ts, "open": 1, "high": 2, "low": 0.5,
        "close": 1.5, "volume": 100, "confirmed": True,
    })

    assert manager._is_data_stale(warmup_mock, staleness_candles=5) is False


@pytest.mark.asyncio
async def test_is_data_stale_uses_tf_scaled_silence_threshold():
    cache = SharedCandleCache()
    cache.register_data_requirement("BTCUSDT", "1m", warmup_bars=5, retain_bars=10)

    redis_mock = MagicMock()
    warmup_mock = MagicMock()
    warmup_mock.get_required_tfs = MagicMock(return_value=["1m"])

    manager = SharedPubSubManager(redis_mock, cache)

    fresh_ts = int(time.time() * 1000) - 100
    cache.upsert_candle("BTCUSDT", "1m", {
        "open_time": fresh_ts, "open": 1, "high": 2, "low": 0.5,
        "close": 1.5, "volume": 100, "confirmed": True,
    })

    manager._last_message_time = time.monotonic() - 301

    assert manager._is_data_stale(warmup_mock, staleness_candles=5) is True


@pytest.mark.asyncio
async def test_is_data_stale_does_not_mark_hourly_silent_for_60s():
    cache = SharedCandleCache()
    cache.register_data_requirement("BTCUSDT", "1h", warmup_bars=5, retain_bars=10)

    redis_mock = MagicMock()
    warmup_mock = MagicMock()
    warmup_mock.get_required_tfs = MagicMock(return_value=["1h"])

    manager = SharedPubSubManager(redis_mock, cache)

    fresh_ts = int(time.time() * 1000) - 100
    cache.upsert_candle("BTCUSDT", "1h", {
        "open_time": fresh_ts, "open": 1, "high": 2, "low": 0.5,
        "close": 1.5, "volume": 100, "confirmed": True,
    })

    manager._last_message_time = time.monotonic() - 61

    assert manager._is_data_stale(warmup_mock, staleness_candles=5) is False


@pytest.mark.asyncio
async def test_is_data_stale_returns_false_when_messages_recent():
    cache = SharedCandleCache()
    cache.register_data_requirement("BTCUSDT", "1m", warmup_bars=5, retain_bars=10)

    redis_mock = MagicMock()
    warmup_mock = MagicMock()
    warmup_mock.get_required_tfs = MagicMock(return_value=["1m"])

    manager = SharedPubSubManager(redis_mock, cache)

    fresh_ts = int(time.time() * 1000) - 100
    cache.upsert_candle("BTCUSDT", "1m", {
        "open_time": fresh_ts, "open": 1, "high": 2, "low": 0.5,
        "close": 1.5, "volume": 100, "confirmed": True,
    })

    manager._last_message_time = time.monotonic() - 5

    assert manager._is_data_stale(warmup_mock, staleness_candles=5) is False


@pytest.mark.asyncio
async def test_find_stale_symbols_identifies_symbols_with_old_data():
    cache = SharedCandleCache()
    cache.register_data_requirement("BTCUSDT", "1m", warmup_bars=5, retain_bars=10)
    cache.register_data_requirement("ETHUSDT", "1m", warmup_bars=5, retain_bars=10)

    redis_mock = MagicMock()
    warmup_mock = MagicMock()
    warmup_mock.get_required_tfs = MagicMock(return_value=["1m"])

    manager = SharedPubSubManager(redis_mock, cache)

    stale_ts = int(time.time() * 1000) - 900_000
    cache.upsert_candle("BTCUSDT", "1m", {
        "open_time": stale_ts, "open": 1, "high": 2, "low": 0.5,
        "close": 1.5, "volume": 100, "confirmed": True,
    })
    fresh_ts = int(time.time() * 1000) - 100
    cache.upsert_candle("ETHUSDT", "1m", {
        "open_time": fresh_ts, "open": 1, "high": 2, "low": 0.5,
        "close": 1.5, "volume": 100, "confirmed": True,
    })

    stale = manager._find_stale_symbols(warmup_mock, staleness_candles=5)
    stale_keys = [(s, t) for s, t in stale]
    assert ("BTCUSDT", "1m") in stale_keys
    assert ("ETHUSDT", "1m") not in stale_keys


def test_trading_session_active_within_window():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from runner.config import TradingSession

    session = TradingSession(
        start="08:45", end="14:25", timezone="Asia/Ho_Chi_Minh", trade_weekends=False
    )
    # Thursday 10:00 Asia/Ho_Chi_Minh — inside the session.
    now = datetime(2026, 8, 6, 10, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    assert session.is_active(now) is True


def test_trading_session_inactive_outside_window():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from runner.config import TradingSession

    session = TradingSession(
        start="08:45", end="14:25", timezone="Asia/Ho_Chi_Minh", trade_weekends=False
    )
    # Thursday 18:00 Asia/Ho_Chi_Minh — market closed.
    now = datetime(2026, 8, 6, 18, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    assert session.is_active(now) is False


def test_trading_session_inactive_on_weekend():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from runner.config import TradingSession

    session = TradingSession(
        start="08:45", end="14:25", timezone="Asia/Ho_Chi_Minh", trade_weekends=False
    )
    # Saturday 10:00 — weekend, closed even inside the window.
    now = datetime(2026, 8, 8, 10, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    assert session.is_active(now) is False


def test_trading_session_none_means_always_active():
    from runner.config import TradingSession

    assert TradingSession().is_active() is True


def test_outside_trading_session_skips_reconnect():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from runner.config import TradingSession

    cache = SharedCandleCache()
    cache.register_data_requirement("41I1G8000", "5m", warmup_bars=5, retain_bars=10)
    stale_ts = int(time.time() * 1000) - 900_000
    cache.upsert_candle("41I1G8000", "5m", {
        "open_time": stale_ts, "open": 1, "high": 2, "low": 0.5,
        "close": 1.5, "volume": 100, "confirmed": True,
    })

    redis_mock = MagicMock()
    warmup_mock = MagicMock()
    warmup_mock.get_required_tfs = MagicMock(return_value=["5m"])

    manager = SharedPubSubManager(redis_mock, cache)
    session = TradingSession(
        start="08:45", end="14:25", timezone="Asia/Ho_Chi_Minh", trade_weekends=False
    )
    # Monkeypatch is_active to simulate market closed.
    session_closed = TradingSession(
        start="08:45", end="14:25", timezone="Asia/Ho_Chi_Minh", trade_weekends=False
    )
    import runner.config as config_module
    orig = TradingSession.is_active
    TradingSession.is_active = lambda self, now=None: False
    try:
        manager.set_reconnect_handler(warmup_mock, staleness_candles=5, trading_session=session_closed)
        assert manager._outside_trading_session() is True
    finally:
        TradingSession.is_active = orig
    assert session.is_active() is not None  # sanity: original method still works

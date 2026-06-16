from __future__ import annotations

import json

from runner.data_layer.snapshot import SnapshotReader


class FakeRedis:
    def __init__(self):
        self.lists = {}
        self.hashes = {}
        self.fail = False

    def lrange(self, key, start, end):
        if self.fail:
            raise RuntimeError("redis down")
        values = self.lists.get(key, [])
        if end == -1:
            end = len(values) - 1
        return values[start:end + 1]

    def hgetall(self, key):
        if self.fail:
            raise RuntimeError("redis down")
        return self.hashes.get(key, {})


def _candle(seconds, close=1):
    return {
        "open_time": seconds * 1000,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1,
    }


def _raw(seconds, close=1):
    return json.dumps(_candle(seconds, close))


def test_snapshot_loads_fresh_v2_list():
    redis = FakeRedis()
    redis.lists["kline_snapshot_v2:binance:15m:BTCUSDT"] = [
        _raw(1_000_000),
        _raw(999_100),
    ]
    reader = SnapshotReader(redis, "binance", now_func=lambda: 1_000_100)

    candles = reader.load("BTCUSDT", "15m", 2)

    assert [c["open_time"] for c in candles] == [999_100_000, 1_000_000_000]


def test_snapshot_uses_latest_values_when_list_has_more_than_requested():
    redis = FakeRedis()
    redis.lists["kline_snapshot_v2:binance:15m:BTCUSDT"] = [
        _raw(1_000_000, 5),
        _raw(999_100, 4),
        _raw(998_200, 3),
        _raw(997_300, 2),
    ]
    reader = SnapshotReader(redis, "binance", now_func=lambda: 1_000_100)

    candles = reader.load("BTCUSDT", "15m", 2)

    assert [c["close"] for c in candles] == [4, 5]


def test_snapshot_rejects_insufficient_bars():
    redis = FakeRedis()
    redis.lists["kline_snapshot_v2:binance:15m:BTCUSDT"] = [_raw(1_000_000)]
    reader = SnapshotReader(redis, "binance", now_func=lambda: 1_000_100)

    assert reader.load("BTCUSDT", "15m", 2) is None


def test_snapshot_rejects_stale_latest_candle():
    redis = FakeRedis()
    redis.lists["kline_snapshot_v2:binance:15m:BTCUSDT"] = [
        _raw(1_000_000),
        _raw(999_100),
    ]
    reader = SnapshotReader(redis, "binance", now_func=lambda: 1_002_000)

    assert reader.load("BTCUSDT", "15m", 2) is None


def test_snapshot_falls_back_to_legacy_hash_when_v2_empty():
    redis = FakeRedis()
    redis.hashes["kline_snapshot:binance:15m:BTCUSDT"] = {
        "a": _raw(999_100),
        "b": _raw(1_000_000),
    }
    reader = SnapshotReader(redis, "binance", now_func=lambda: 1_000_100)

    candles = reader.load("BTCUSDT", "15m", 2)

    assert [c["open_time"] for c in candles] == [999_100_000, 1_000_000_000]


def test_snapshot_prefers_v2_over_legacy_when_both_exist():
    redis = FakeRedis()
    redis.lists["kline_snapshot_v2:binance:15m:BTCUSDT"] = [
        _raw(1_000_000, 10),
        _raw(999_100, 9),
    ]
    redis.hashes["kline_snapshot:binance:15m:BTCUSDT"] = {
        "a": _raw(999_100, 1),
        "b": _raw(1_000_000, 2),
    }
    reader = SnapshotReader(redis, "binance", now_func=lambda: 1_000_100)

    candles = reader.load("BTCUSDT", "15m", 2)

    assert [c["close"] for c in candles] == [9, 10]


def test_snapshot_returns_none_on_redis_exception():
    redis = FakeRedis()
    redis.fail = True
    reader = SnapshotReader(redis, "binance", now_func=lambda: 1_000_100)

    assert reader.load("BTCUSDT", "15m", 2) is None

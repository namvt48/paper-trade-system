from __future__ import annotations

import json

from runner.data_layer.funding_snapshot import FundingSnapshotReader


class FakeRedis:
    def __init__(self):
        self.lists = {}
        self.fail = False

    def lrange(self, key, start, end):
        if self.fail:
            raise RuntimeError("redis down")
        values = self.lists.get(key, [])
        if end == -1:
            end = len(values) - 1
        return values[start:end + 1]


def _row(funding_time, funding_rate):
    return json.dumps({
        "symbol": "BTCUSDT", "exchange": "binance",
        "funding_time": funding_time, "funding_rate": funding_rate,
    })


def test_funding_snapshot_loads_and_sorts_by_time():
    redis = FakeRedis()
    redis.lists["funding_snapshot:binance:BTCUSDT"] = [
        _row(2_000, 0.0002),
        _row(1_000, 0.0001),
    ]
    reader = FundingSnapshotReader(redis, "binance")

    rows = reader.load("BTCUSDT")

    assert [r["funding_time"] for r in rows] == [1_000, 2_000]
    assert [r["funding_rate"] for r in rows] == [0.0001, 0.0002]


def test_funding_snapshot_dedupes_by_time():
    redis = FakeRedis()
    redis.lists["funding_snapshot:binance:BTCUSDT"] = [
        _row(1_000, 0.0005),  # stale duplicate for the same funding_time
        _row(1_000, 0.0001),
        _row(2_000, 0.0002),
    ]
    reader = FundingSnapshotReader(redis, "binance")

    rows = reader.load("BTCUSDT")

    assert len(rows) == 2
    assert [r["funding_time"] for r in rows] == [1_000, 2_000]


def test_funding_snapshot_missing_symbol_returns_none():
    redis = FakeRedis()
    reader = FundingSnapshotReader(redis, "binance")

    assert reader.load("ETHUSDT") is None


def test_funding_snapshot_returns_none_on_redis_error():
    redis = FakeRedis()
    redis.fail = True
    reader = FundingSnapshotReader(redis, "binance")

    assert reader.load("BTCUSDT") is None


def test_funding_snapshot_returns_none_on_undecodable_json():
    redis = FakeRedis()
    redis.lists["funding_snapshot:binance:BTCUSDT"] = [b"not json"]
    reader = FundingSnapshotReader(redis, "binance")

    assert reader.load("BTCUSDT") is None

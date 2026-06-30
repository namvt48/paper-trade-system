import asyncio
import json
import pytest
from runner.data_layer.mds_ready import MDSReadyWatcher, ReadySignal


def _ready_payload(tf="15m", complete_count=280, partial_count=10, insufficient_count=10,
                    partial_symbols=None, insufficient_symbols=None):
    return {
        "status": "ok",
        "exchange": "binance",
        "tf": tf,
        "timestamp": 1718449200000,
        "complete_count": complete_count,
        "partial_count": partial_count,
        "insufficient_count": insufficient_count,
        "partial_symbols": partial_symbols or {},
        "insufficient_symbols": insufficient_symbols or [],
    }


class FakeRedis:
    _runner_inline_redis = True

    def __init__(self):
        self._keys = {}

    def set(self, key, value, ex=None):
        self._keys[key] = value

    def get(self, key):
        return self._keys.get(key)


@pytest.mark.asyncio
async def test_wait_for_ready_reads_existing_key():
    redis = FakeRedis()
    payload = _ready_payload()
    redis.set("mds:warmup:ready:binance:15m", json.dumps(payload))

    watcher = MDSReadyWatcher(redis, exchange="binance")
    result = await watcher.wait_for_ready(required_tfs=["15m"], timeout_sec=10)

    assert "15m" in result
    assert result["15m"].complete_count == 280
    assert result["15m"].partial_symbols == {}


@pytest.mark.asyncio
async def test_wait_for_ready_timeout():
    redis = FakeRedis()

    watcher = MDSReadyWatcher(redis, exchange="binance")
    result = await watcher.wait_for_ready(required_tfs=["15m"], timeout_sec=0.1)

    assert result == {}


@pytest.mark.asyncio
async def test_process_ready_signal_classifies_symbols():
    redis = FakeRedis()
    payload = _ready_payload(
        partial_symbols={"SOLUSDT": 0.71},
        insufficient_symbols=["NEWUSDT"],
    )
    redis.set("mds:warmup:ready:binance:15m", json.dumps(payload))

    watcher = MDSReadyWatcher(redis, exchange="binance")
    result = await watcher.wait_for_ready(required_tfs=["15m"], timeout_sec=10)

    assert "NEWUSDT" in result["15m"].insufficient_symbols
    assert result["15m"].partial_symbols == {"SOLUSDT": 0.71}


def test_ready_signal_from_dict():
    payload = _ready_payload(
        partial_symbols={"SOLUSDT": 0.71},
        insufficient_symbols=["NEWUSDT"],
    )
    signal = ReadySignal.from_dict(payload)
    assert signal.tf == "15m"
    assert signal.complete_count == 280
    assert signal.partial_symbols == {"SOLUSDT": 0.71}
    assert signal.insufficient_symbols == ["NEWUSDT"]

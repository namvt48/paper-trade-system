from __future__ import annotations

import pytest

from runner.data_layer.cache import SharedCandleCache
from runner.lease import LeaseManager
from runner.reconcile.state import StrategyRuntimeState
from runner.signal.dispatcher import SignalDispatcher
from runner.strategy.context import StrategyContext


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.xadds = []

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)

    def expire(self, key, ttl):
        return key in self.values

    def delete(self, key):
        return bool(self.values.pop(key, None) is not None)

    def xadd(self, stream, fields):
        self.xadds.append((stream, dict(fields)))
        return "1-0"


def make_ctx(dispatcher):
    return StrategyContext("a", "v1", SharedCandleCache(), dispatcher, StrategyRuntimeState(ready=True))


def test_lease_acquire_renew_release_and_validity():
    redis = FakeRedis()
    lease = LeaseManager(redis, "runner")

    assert lease.acquire("a") is True
    assert lease.is_valid("a") is True
    assert lease.renew("a") is True
    assert lease.release("a") is True
    assert lease.is_valid("a") is False


@pytest.mark.asyncio
async def test_dispatcher_valid_lease_pushes_signal_and_dedups():
    redis = FakeRedis()
    lease = LeaseManager(redis, "runner")
    lease.acquire("a")
    dispatcher = SignalDispatcher(redis, "paper-signals", lease)
    ctx = make_ctx(dispatcher)
    fields = {
        "symbol": "BTCUSDT", "tf": "15m", "side": "LONG",
        "signal_candle_open_ms": 1000, "reason": "R",
    }

    signal_id = await dispatcher.dispatch(ctx, "OPEN", **fields)
    duplicate = await dispatcher.dispatch(ctx, "OPEN", **fields)
    different = await dispatcher.dispatch(ctx, "OPEN", **{**fields, "signal_candle_open_ms": 2000})

    assert signal_id is not None
    assert duplicate is None
    assert different is not None
    assert len(redis.xadds) == 2
    assert "signal_id" in redis.xadds[0][1]


@pytest.mark.asyncio
async def test_dispatcher_invalid_lease_drops_and_marks_state():
    redis = FakeRedis()
    lease = LeaseManager(redis, "runner")
    dispatcher = SignalDispatcher(redis, "paper-signals", lease)
    ctx = make_ctx(dispatcher)

    result = await dispatcher.dispatch(ctx, "OPEN", symbol="BTCUSDT")

    assert result is None
    assert redis.xadds == []
    assert ctx.state.lease_valid is False


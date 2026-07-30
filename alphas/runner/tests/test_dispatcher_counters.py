from __future__ import annotations

import pytest

from runner.data_layer.cache import SharedCandleCache
from runner.lease import LeaseManager
from runner.metrics import RunnerMetrics
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

    def lrange(self, key, start, stop):
        return []

    def pipeline(self, transaction=False):
        return _FakePipe()


class _FakePipe:
    def lpush(self, *a, **k):
        return self

    def ltrim(self, *a, **k):
        return self

    def execute(self):
        return []


def make_ctx(dispatcher, alpha_id="a"):
    return StrategyContext(alpha_id, "v1", SharedCandleCache(), dispatcher,
                           StrategyRuntimeState(ready=True))


def _fields(**over):
    base = {"symbol": "BTCUSDT", "tf": "15m", "side": "LONG",
            "signal_candle_open_ms": 1000, "reason": "R"}
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_successful_dispatch_increments_dispatched_and_published():
    redis = FakeRedis()
    lease = LeaseManager(redis, "runner")
    lease.acquire("a")
    metrics = RunnerMetrics()
    dispatcher = SignalDispatcher(redis, "paper-signals", lease, metrics=metrics)
    ctx = make_ctx(dispatcher)

    result = await dispatcher.dispatch(ctx, "OPEN", **_fields())

    assert result is not None
    assert metrics.signals_dispatched_total == 1
    assert metrics.signals_xadd_published_total == 1
    assert metrics.signals_lease_dropped_total == 0
    assert metrics.signals_dedup_skipped_total == 0


@pytest.mark.asyncio
async def test_lease_invalid_increments_dispatched_and_lease_dropped_not_published():
    redis = FakeRedis()
    lease = LeaseManager(redis, "runner")  # never acquired -> invalid
    metrics = RunnerMetrics()
    dispatcher = SignalDispatcher(redis, "paper-signals", lease, metrics=metrics)
    ctx = make_ctx(dispatcher, alpha_id="daily-x")

    result = await dispatcher.dispatch(ctx, "OPEN", **_fields())

    assert result is None
    assert metrics.signals_dispatched_total == 1
    assert metrics.signals_lease_dropped_total == 1
    assert metrics.signals_lease_dropped_by_alpha == {"daily-x": 1}
    assert metrics.signals_xadd_published_total == 0
    assert redis.xadds == []


@pytest.mark.asyncio
async def test_duplicate_increments_dispatched_and_dedup_not_published():
    redis = FakeRedis()
    lease = LeaseManager(redis, "runner")
    lease.acquire("a")
    metrics = RunnerMetrics()
    dispatcher = SignalDispatcher(redis, "paper-signals", lease, metrics=metrics)
    ctx = make_ctx(dispatcher)

    await dispatcher.dispatch(ctx, "OPEN", **_fields())
    result = await dispatcher.dispatch(ctx, "OPEN", **_fields())  # same -> dedup

    assert result is None
    assert metrics.signals_dispatched_total == 2
    assert metrics.signals_xadd_published_total == 1
    assert metrics.signals_dedup_skipped_total == 1
    assert metrics.signals_dedup_skipped_by_alpha == {"a": 1}


@pytest.mark.asyncio
async def test_invariant_dispatched_equals_dedup_plus_lease_plus_published():
    redis = FakeRedis()
    lease = LeaseManager(redis, "runner")
    lease.acquire("a")
    metrics = RunnerMetrics()
    dispatcher = SignalDispatcher(redis, "paper-signals", lease, metrics=metrics)
    ctx = make_ctx(dispatcher)

    # 2 unique published, 1 dedup
    await dispatcher.dispatch(ctx, "OPEN", **_fields(signal_candle_open_ms=1000))
    await dispatcher.dispatch(ctx, "OPEN", **_fields(signal_candle_open_ms=2000))
    await dispatcher.dispatch(ctx, "OPEN", **_fields(signal_candle_open_ms=1000))
    # 1 lease-dropped on a different, unleased alpha
    ctx2 = make_ctx(dispatcher, alpha_id="no-lease")
    await dispatcher.dispatch(ctx2, "OPEN", **_fields())

    m = metrics
    assert m.signals_dispatched_total == 4
    assert (m.signals_dedup_skipped_total
            + m.signals_lease_dropped_total
            + m.signals_xadd_published_total) == m.signals_dispatched_total


@pytest.mark.asyncio
async def test_metrics_none_does_not_break_dispatch():
    redis = FakeRedis()
    lease = LeaseManager(redis, "runner")
    lease.acquire("a")
    dispatcher = SignalDispatcher(redis, "paper-signals", lease)  # metrics=None
    ctx = make_ctx(dispatcher)

    result = await dispatcher.dispatch(ctx, "OPEN", **_fields())

    assert result is not None
    assert len(redis.xadds) == 1

from __future__ import annotations

import asyncio
import json

import pytest
from redis.exceptions import TimeoutError as RedisTimeoutError

from runner.data_layer.cache import SharedCandleCache
from runner.data_layer.warmup import (
    MDSWarmupBackend,
    WarmupManager,
    WarmupRequirement,
    bars_bucket,
)
from runner.metrics import RunnerMetrics


class StrategyStub:
    def __init__(self, symbols, tf="15m", bars=2, alpha_id="strategy", retain_bars=None, retain_buffer_bars=0):
        self.symbols = symbols
        self.tf = tf
        self.bars = bars
        self.alpha_id = alpha_id
        self.retain_bars = retain_bars
        self.retain_buffer_bars = retain_buffer_bars

    def get_warmup_symbols(self): return self.symbols
    def get_warmup_tfs(self): return [self.tf]
    def get_warmup_bars(self, tf): return self.bars
    def get_retain_bars(self, tf): return self.bars if self.retain_bars is None else self.retain_bars
    def get_retain_buffer_bars(self, tf): return self.retain_buffer_bars


class SnapshotStub:
    def __init__(self, candles_by_key):
        self.candles_by_key = candles_by_key
        self.calls = []

    def load(self, symbol, tf, bars):
        self.calls.append((symbol, tf, bars))
        candles = self.candles_by_key.get((symbol, tf))
        if candles is None:
            return None
        return candles[-bars:]


def _candles(bars=2, start=1_000_000):
    return [
        {
            "open_time": (start + i * 60) * 1000,
            "open": 1,
            "high": 2,
            "low": 0,
            "close": 1,
            "volume": 1,
        }
        for i in range(bars)
    ]


def _load(cache, symbol, bars=2, tf="15m"):
    cache.register_bars_requirement(symbol, tf, bars)
    for candle in _candles(bars):
        cache.upsert_candle(symbol, tf, candle)


@pytest.mark.asyncio
async def test_warmup_union_max_bars_and_buckets():
    async def backend(reqs):
        return {(r.symbol, r.tf) for r in reqs}

    manager = WarmupManager(SharedCandleCache(), backend)
    reqs = manager.collect_requirements([
        StrategyStub(["BTCUSDT"], bars=100),
        StrategyStub(["BTCUSDT"], bars=600),
    ])

    assert reqs == {("BTCUSDT", "15m"): 600}
    assert bars_bucket(500) == "le_500"
    assert bars_bucket(501) == "le_2000"
    assert bars_bucket(2001) == "gt_2000"


@pytest.mark.asyncio
async def test_warmup_cache_hit_skips_snapshot_and_backend_and_readiness_is_per_strategy():
    calls = 0

    async def backend(reqs):
        nonlocal calls
        calls += 1
        return set()

    cache = SharedCandleCache()
    snapshot = SnapshotStub({("BTCUSDT", "15m"): _candles(5)})
    manager = WarmupManager(cache, backend, snapshot_reader=snapshot)
    small = StrategyStub(["BTCUSDT"], bars=2)
    large = StrategyStub(["BTCUSDT"], bars=5)
    _load(cache, "BTCUSDT", bars=2)

    assert manager.strategy_ready(small, 0.90) is True
    assert manager.strategy_ready(large, 0.90) is False
    await manager.request_warmup({("BTCUSDT", "15m"): 2})
    assert calls == 0
    assert snapshot.calls == []


@pytest.mark.asyncio
async def test_snapshot_hit_path_sends_zero_mds_requests():
    calls = 0

    async def backend(reqs):
        nonlocal calls
        calls += 1
        return set()

    cache = SharedCandleCache()
    snapshot = SnapshotStub({("BTCUSDT", "15m"): _candles(3)})
    metrics = RunnerMetrics()
    manager = WarmupManager(cache, backend, snapshot_reader=snapshot, metrics=metrics)

    loaded = await manager.request_warmup({("BTCUSDT", "15m"): 3})

    assert loaded == {("BTCUSDT", "15m")}
    assert calls == 0
    assert cache.get_bar_count("BTCUSDT", "15m") == 3
    assert metrics.warmup_snapshot_hits_total == 1


@pytest.mark.asyncio
async def test_partial_snapshot_hit_sends_mds_only_for_missing_symbols():
    calls = []

    async def backend(reqs):
        calls.append(reqs)
        return {(req.symbol, req.tf): _candles(req.bars) for req in reqs}

    cache = SharedCandleCache()
    snapshot = SnapshotStub({("BTCUSDT", "15m"): _candles(3)})
    manager = WarmupManager(cache, backend, snapshot_reader=snapshot)

    loaded = await manager.request_warmup({
        ("BTCUSDT", "15m"): 3,
        ("ETHUSDT", "15m"): 3,
    })

    assert loaded == {("BTCUSDT", "15m"), ("ETHUSDT", "15m")}
    assert len(calls) == 1
    assert calls[0] == (WarmupRequirement("ETHUSDT", "15m", 3),)


@pytest.mark.asyncio
async def test_snapshot_large_bars_has_no_500_bar_gate():
    async def backend(reqs):
        raise AssertionError("snapshot should satisfy large warmup")

    cache = SharedCandleCache()
    snapshot = SnapshotStub({("BTCUSDT", "15m"): _candles(8641)})
    manager = WarmupManager(cache, backend, snapshot_reader=snapshot)

    loaded = await manager.request_warmup({("BTCUSDT", "15m"): 8641})

    assert loaded == {("BTCUSDT", "15m")}
    assert cache.get_bar_count("BTCUSDT", "15m") == 8641


@pytest.mark.asyncio
async def test_insufficient_cache_triggers_snapshot_then_mds_if_needed():
    calls = []

    async def backend(reqs):
        calls.append(reqs)
        return {(req.symbol, req.tf): _candles(req.bars) for req in reqs}

    cache = SharedCandleCache()
    _load(cache, "BTCUSDT", bars=2)
    manager = WarmupManager(cache, backend, snapshot_reader=SnapshotStub({}))

    loaded = await manager.request_warmup({("BTCUSDT", "15m"): 5})

    assert loaded == {("BTCUSDT", "15m")}
    assert len(calls) == 1
    assert calls[0] == (WarmupRequirement("BTCUSDT", "15m", 5),)


@pytest.mark.asyncio
async def test_warmup_coalesces_identical_inflight_request():
    calls = 0

    async def backend(reqs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return {(r.symbol, r.tf): _candles(r.bars) for r in reqs}

    manager = WarmupManager(SharedCandleCache(), backend)
    reqs = {("BTCUSDT", "15m"): 2}

    one, two = await asyncio.gather(manager.request_warmup(reqs), manager.request_warmup(reqs))

    assert one == {("BTCUSDT", "15m")}
    assert two == {("BTCUSDT", "15m")}
    assert calls == 1


def test_warmup_groups_missing_by_tf_and_bars_bucket_are_deterministic():
    manager = WarmupManager(SharedCandleCache(), lambda reqs: None)
    grouped = manager.group_missing_by_bucket([
        WarmupRequirement("ETHUSDT", "15m", 600),
        WarmupRequirement("BTCUSDT", "15m", 100),
        WarmupRequirement("SOLUSDT", "1h", 600),
    ])

    assert list(grouped) == [("15m", "le_2000"), ("15m", "le_500"), ("1h", "le_2000")]
    assert grouped[("15m", "le_500")] == (WarmupRequirement("BTCUSDT", "15m", 100),)


def test_same_symbol_tf_does_not_appear_in_two_mds_batches_after_max_dedupe():
    manager = WarmupManager(SharedCandleCache(), lambda reqs: None)
    reqs = manager.collect_requirements([
        StrategyStub(["BTCUSDT"], bars=400),
        StrategyStub(["BTCUSDT"], bars=8641),
    ])
    grouped = manager.group_missing_by_bucket(manager.missing_requirements(reqs))

    assert reqs == {("BTCUSDT", "15m"): 8641}
    assert grouped == {("15m", "gt_2000"): (WarmupRequirement("BTCUSDT", "15m", 8641),)}


@pytest.mark.asyncio
async def test_warmup_chunks_large_mds_batches_by_symbol_limit():
    calls = []

    async def backend(reqs):
        calls.append(reqs)
        return {(req.symbol, req.tf): _candles(req.bars) for req in reqs}

    manager = WarmupManager(
        SharedCandleCache(),
        backend,
        max_symbols_per_mds_request=2,
    )

    loaded = await manager.request_warmup({
        ("BTCUSDT", "15m"): 8641,
        ("ETHUSDT", "15m"): 8641,
        ("SOLUSDT", "15m"): 8641,
        ("XRPUSDT", "15m"): 8641,
        ("BNBUSDT", "15m"): 8641,
    })

    assert loaded == {
        ("BTCUSDT", "15m"),
        ("ETHUSDT", "15m"),
        ("SOLUSDT", "15m"),
        ("XRPUSDT", "15m"),
        ("BNBUSDT", "15m"),
    }
    assert [len(call) for call in calls] == [2, 2, 1]


def test_collect_requirements_aggregates_warmup_and_retention_separately():
    cache = SharedCandleCache()
    manager = WarmupManager(cache, lambda reqs: None)

    reqs = manager.collect_requirements([
        StrategyStub(["BTCUSDT"], bars=8000, retain_bars=8000),
        StrategyStub(["BTCUSDT"], bars=1000, retain_bars=600, retain_buffer_bars=50),
        StrategyStub(["ETHUSDT"], bars=500),
    ])

    assert reqs == {
        ("BTCUSDT", "15m"): 8000,
        ("ETHUSDT", "15m"): 500,
    }
    assert cache.required_bars("BTCUSDT", "15m") == 8000
    assert cache.retained_bars("BTCUSDT", "15m") == 8000
    assert cache.required_bars("ETHUSDT", "15m") == 500
    assert cache.retained_bars("ETHUSDT", "15m") == 500


def test_small_strategy_readiness_independent_from_large_strategy():
    cache = SharedCandleCache()
    manager = WarmupManager(cache, lambda reqs: None)
    small = StrategyStub(["BTCUSDT"], bars=400)
    large = StrategyStub(["BTCUSDT"], bars=8641)
    _load(cache, "BTCUSDT", bars=400)

    assert manager.strategy_ready(small) is True
    assert manager.strategy_ready(large) is False


@pytest.mark.asyncio
async def test_response_cache_prevents_duplicate_immediate_request_only_when_candles_exist():
    calls = 0

    async def backend(reqs):
        nonlocal calls
        calls += 1
        return {(req.symbol, req.tf): _candles(req.bars) for req in reqs}

    cache = SharedCandleCache()
    manager = WarmupManager(cache, backend, response_cache_ttl_sec=300)
    reqs = {("BTCUSDT", "15m"): 2}

    await manager.request_warmup(reqs)
    await manager.request_warmup(reqs)
    assert calls == 1

    empty_cache = SharedCandleCache()
    manager.cache = empty_cache
    await manager.request_warmup(reqs)
    assert calls == 2


@pytest.mark.asyncio
async def test_expired_response_cache_allows_new_request():
    calls = 0
    now = 0.0

    def now_func():
        return now

    async def backend(reqs):
        nonlocal calls
        calls += 1
        return {(req.symbol, req.tf): _candles(req.bars) for req in reqs}

    manager = WarmupManager(
        SharedCandleCache(),
        backend,
        response_cache_ttl_sec=1,
        now_func=now_func,
    )
    reqs = {("BTCUSDT", "15m"): 2}

    await manager.request_warmup(reqs)
    now = 2.0
    await manager.request_warmup({("ETHUSDT", "15m"): 2})
    manager.cache = SharedCandleCache()
    await manager.request_warmup(reqs)

    assert calls == 3


@pytest.mark.asyncio
async def test_semaphore_limits_concurrency():
    active = 0
    max_active = 0

    async def backend(reqs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {(req.symbol, req.tf): _candles(req.bars) for req in reqs}

    manager = WarmupManager(SharedCandleCache(), backend, max_concurrent_mds_requests=1)
    await asyncio.gather(
        manager.request_warmup({("BTCUSDT", "15m"): 2}),
        manager.request_warmup({("ETHUSDT", "15m"): 2}),
    )

    assert max_active == 1


@pytest.mark.asyncio
async def test_minute_limiter_delays_next_request():
    now = 0.0
    sleeps = []

    def now_func():
        return now

    async def sleep_func(delay):
        nonlocal now
        sleeps.append(delay)
        now += delay

    async def backend(reqs):
        return {(req.symbol, req.tf): _candles(req.bars) for req in reqs}

    manager = WarmupManager(
        SharedCandleCache(),
        backend,
        max_mds_requests_per_minute=1,
        now_func=now_func,
        sleep_func=sleep_func,
    )

    await manager.request_warmup({("BTCUSDT", "15m"): 2})
    await manager.request_warmup({("ETHUSDT", "15m"): 2})

    assert sleeps == [60.0]


@pytest.mark.asyncio
async def test_timeout_returns_partial_without_exception():
    async def backend(reqs):
        await asyncio.sleep(0.05)
        return {(req.symbol, req.tf): _candles(req.bars) for req in reqs}

    metrics = RunnerMetrics()
    manager = WarmupManager(
        SharedCandleCache(),
        backend,
        metrics=metrics,
        request_timeout_sec=0.001,
    )

    loaded = await manager.request_warmup({("BTCUSDT", "15m"): 2})

    assert loaded == set()
    assert metrics.warmup_timeouts_total == 1


@pytest.mark.asyncio
async def test_partial_backend_result_keeps_received_candles_and_marks_partial():
    async def backend(reqs):
        return {("BTCUSDT", "15m"): _candles(2)}

    metrics = RunnerMetrics()
    cache = SharedCandleCache()
    manager = WarmupManager(cache, backend, metrics=metrics)

    loaded = await manager.request_warmup({
        ("BTCUSDT", "15m"): 2,
        ("ETHUSDT", "15m"): 2,
    })

    assert loaded == {("BTCUSDT", "15m")}
    assert cache.get_bar_count("BTCUSDT", "15m") == 2
    assert cache.get_bar_count("ETHUSDT", "15m") == 0
    assert metrics.warmup_partial_ready_total == 1
    assert metrics.warmup_timeouts_total == 1


def test_partial_warmup_marks_ready_only_when_coverage_reaches_90_percent():
    cache = SharedCandleCache()
    manager = WarmupManager(cache, lambda reqs: None)
    symbols = [f"S{i}" for i in range(10)]
    strategy = StrategyStub(symbols, bars=2)
    for symbol in symbols[:8]:
        _load(cache, symbol, bars=2)

    assert manager.strategy_ready(strategy, min_coverage=0.90) is False
    for symbol in symbols[8:9]:
        _load(cache, symbol, bars=2)
    assert manager.strategy_ready(strategy, min_coverage=0.90) is True


class FakeRedis:
    def __init__(self):
        self.xadds = []

    def xadd(self, stream, fields):
        self.xadds.append((stream, fields))

    def xread(self, streams, count=None, block=None):
        response_stream = next(iter(streams))
        _stream, request = self.xadds[-1]
        expected_stream = request["response_stream"]
        if response_stream != expected_stream:
            return []
        entries = []
        for index, symbol in enumerate(request["symbols"].split(","), start=1):
            entries.append((
                f"{index}-0",
                {"symbol": symbol, "tf": request["tf"], "candles": json.dumps(_candles(int(request["bars"])))},
            ))
        return [(response_stream, entries[:count])]


def test_mds_request_contract_matches_existing_consumer_fields():
    redis = FakeRedis()
    backend = MDSWarmupBackend(redis, "binance", "runner-1", timeout_sec=0.01)
    reqs = (
        WarmupRequirement("ETHUSDT", "15m", 500),
        WarmupRequirement("BTCUSDT", "15m", 500),
    )

    stream, fields, response_stream, symbols = backend.build_request(reqs)

    assert stream == "warmup:request:binance"
    assert fields["alpha_id"]
    assert fields["response_stream"] == response_stream
    assert fields["tf"] == "15m"
    assert fields["bars"] == "500"
    assert fields["symbols"] == "BTCUSDT,ETHUSDT"
    assert json.loads(fields["symbols_json"]) == ["BTCUSDT", "ETHUSDT"]
    assert symbols == ["BTCUSDT", "ETHUSDT"]


@pytest.mark.asyncio
async def test_mds_reader_tolerates_transient_redis_socket_timeout():
    class TimeoutThenResponseRedis(FakeRedis):
        def __init__(self):
            super().__init__()
            self._runner_inline_redis = True
            self.reads = 0

        def xread(self, streams, count=None, block=None):
            self.reads += 1
            if self.reads == 1:
                raise RedisTimeoutError("Timeout reading from socket")
            return super().xread(streams, count=count, block=block)

    redis = TimeoutThenResponseRedis()
    backend = MDSWarmupBackend(redis, "binance", "runner-1", timeout_sec=1.0)
    result = await backend((WarmupRequirement("BTCUSDT", "15m", 2),))

    assert ("BTCUSDT", "15m") in result

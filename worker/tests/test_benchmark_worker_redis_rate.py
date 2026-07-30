"""Integration contract for the isolated sustained-rate Redis benchmark."""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import asdict
from pathlib import Path

import anyio
import pytest
import redis
from redis.asyncio import Redis as AsyncRedis
from redis.exceptions import ConnectionError as RedisConnectionError

from benchmarks import worker_redis_rate_stream
from benchmarks.worker_redis_models import RedisBenchmarkConfig
from benchmarks.worker_redis_rate import (
    InvalidOfferedRate,
    RateBenchmarkConfig,
    RateBenchmarkResult,
    run_rate_benchmark,
)
from benchmarks.worker_redis_runtime import _cleanup_redis
from benchmarks.worker_redis_stream import GroupState
from benchmarks.worker_sqlite import BenchmarkConfig, LatencySummary


def test_rate_config_rejects_nonpositive_offered_rate() -> None:
    # Given an offered rate that cannot define a forward producer schedule.
    # When the rate benchmark boundary parses it.
    with pytest.raises(InvalidOfferedRate):
        _ = RateBenchmarkConfig(
            redis=RedisBenchmarkConfig(),
            offered_rate=0.0,
        )
    # Then construction fails before any Redis connection is attempted.


def test_rate_result_serializes_without_target_or_namespace() -> None:
    # Given a complete typed rate result.
    latency = LatencySummary(count=8, avg=1.0, max=2.0, p50=1.0, p95=2.0, p99=2.0)
    result = RateBenchmarkResult(
        isolated=True,
        scope="worker-redis-sustained-rate",
        clock_source="test",
        alpha_count=2,
        positions_per_alpha=2,
        cycles=1,
        offered_rate=40.0,
        achieved_produce_rate=39.5,
        signal_count=8,
        published_count=8,
        received_count=8,
        committed_count=8,
        acked_count=8,
        error_count=0,
        trade_count=4,
        open_position_count=4,
        producer_finished_acked_count=3,
        producer_end_pending=1,
        producer_end_lag=4,
        producer_end_backlog=5,
        producer_end_backlog_ratio=0.625,
        final_pending=0,
        final_lag=0,
        final_backlog=0,
        produce_duration_sec=0.2,
        total_duration_sec=0.3,
        post_producer_drain_duration_sec=0.1,
        consumer_throughput_signals_per_sec=26.67,
        queue_latency_ms=latency,
        end_to_end_latency_ms=latency,
        service_latency_ms=latency,
        xack_rtt_ms=latency,
    )
    # When the CLI payload is serialized.
    payload = json.loads(result.to_json())
    # Then it is complete but does not disclose the Redis target or generated names.
    assert payload == asdict(result)
    assert "redis_url" not in payload
    assert "namespace" not in payload


def test_rate_benchmark_consumes_concurrently_and_drains(tmp_path: Path) -> None:
    # Given a paced workload and an owned real Redis endpoint.
    redis_url = os.environ.get("REDIS_BENCHMARK_URL")
    if redis_url is None:
        pytest.skip("REDIS_BENCHMARK_URL is required for the real Redis integration")
    redis_config = RedisBenchmarkConfig(
        redis_url=redis_url,
        workload=BenchmarkConfig(alpha_count=2, positions_per_alpha=2, cycles=1),
    )
    config = RateBenchmarkConfig(redis=redis_config, offered_rate=20.0)

    # When the producer and serial worker consumer run in one AnyIO task group.
    result = anyio.run(run_rate_benchmark, config, tmp_path)

    # Then consumption starts before publishing finishes and all state drains exactly.
    assert result.isolated is True
    assert result.scope == "worker-redis-sustained-rate"
    assert result.signal_count == 8
    assert (
        result.published_count,
        result.received_count,
        result.committed_count,
        result.acked_count,
    ) == (8, 8, 8, 8)
    assert result.error_count == 0
    assert result.trade_count == 4
    assert result.open_position_count == 4
    assert result.producer_finished_acked_count > 0
    assert (
        result.producer_finished_acked_count + result.producer_end_backlog
        == result.published_count
    )
    assert result.final_pending == 0
    assert result.final_lag == 0
    assert result.final_backlog == 0
    assert result.offered_rate == 20.0
    assert result.achieved_produce_rate > 0.0
    assert result.produce_duration_sec > 0.0
    assert result.total_duration_sec >= result.produce_duration_sec
    assert result.post_producer_drain_duration_sec >= 0.0
    assert result.consumer_throughput_signals_per_sec > 0.0
    for latency in (
        result.queue_latency_ms,
        result.end_to_end_latency_ms,
        result.service_latency_ms,
        result.xack_rtt_ms,
    ):
        assert latency.count == 8
        assert latency.avg >= 0.0
        assert latency.max >= 0.0

    client = redis.Redis.from_url(redis_url, decode_responses=True)
    try:
        assert client.exists(redis_config.namespace.stream) == 0
    finally:
        client.close()
    assert list(tmp_path.iterdir()) == []


def test_rate_benchmark_connection_failure_is_bounded_and_cleans(
    tmp_path: Path,
) -> None:
    # Given the sealed IPv6 target proven to have no listener.
    try:
        with socket.socket(socket.AF_INET6) as probe:
            probe.bind(("::1", 6383))
    except OSError as exc:
        pytest.skip(f"IPv6 loopback port 6383 is unavailable: {exc}")
    config = RateBenchmarkConfig(
        redis=RedisBenchmarkConfig(redis_url="redis://[::1]:6383/14"),
        offered_rate=20.0,
    )

    # When startup cannot reach Redis.
    started = time.perf_counter()
    with pytest.raises(RedisConnectionError):
        _ = anyio.run(run_rate_benchmark, config, tmp_path)

    # Then failure is bounded and no SQLite artifact remains.
    assert time.perf_counter() - started < 2.0
    assert list(tmp_path.iterdir()) == []


def test_rate_producer_snapshot_blocks_ack_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given a delayed Redis group query that opens a deterministic ACK race window.
    redis_url = os.environ.get("REDIS_BENCHMARK_URL")
    if redis_url is None:
        pytest.skip("REDIS_BENCHMARK_URL is required for the real Redis integration")
    config = RateBenchmarkConfig(
        redis=RedisBenchmarkConfig(
            redis_url=redis_url,
            workload=BenchmarkConfig(alpha_count=1, positions_per_alpha=2, cycles=1),
        ),
        offered_rate=10.0,
    )
    real_group_state = worker_redis_rate_stream.group_state

    async def delayed_group_state(
        client: AsyncRedis,
        redis_config: RedisBenchmarkConfig,
    ) -> GroupState:
        await anyio.sleep(0.1)
        return await real_group_state(client, redis_config)

    monkeypatch.setattr(worker_redis_rate_stream, "group_state", delayed_group_state)

    # When the producer captures state while the consumer is otherwise ready to ACK.
    result = anyio.run(run_rate_benchmark, config, tmp_path)

    # Then local ACK and Redis backlog describe the same producer-finished instant.
    assert (
        result.producer_finished_acked_count + result.producer_end_backlog
        == result.published_count
    )


def test_group_state_uses_one_xinfo_group_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a real group with one pending and two lagged entries.
    redis_url = os.environ.get("REDIS_BENCHMARK_URL")
    if redis_url is None:
        pytest.skip("REDIS_BENCHMARK_URL is required for the real Redis integration")
    config = RedisBenchmarkConfig(redis_url=redis_url)

    async def read_snapshot() -> GroupState:
        client = AsyncRedis.from_url(redis_url, decode_responses=True)
        try:
            await client.xgroup_create(
                config.namespace.stream,
                config.namespace.group,
                id="0-0",
                mkstream=True,
            )
            for index in range(3):
                await client.xadd(config.namespace.stream, {"index": str(index)})
            await client.xreadgroup(
                config.namespace.group,
                config.namespace.consumer,
                {config.namespace.stream: ">"},
                count=1,
            )

            async def contradictory_xpending(*_args: str) -> dict[str, int]:
                return {"pending": 99}

            monkeypatch.setattr(client, "xpending", contradictory_xpending)
            return await worker_redis_rate_stream.group_state(client, config)
        finally:
            await _cleanup_redis(client, config, group_created=True)

    # When the shared group-state seam reads Redis.
    snapshot = anyio.run(read_snapshot)

    # Then pending and lag both come from the same XINFO group response.
    assert snapshot == GroupState(pending=1, lag=2)

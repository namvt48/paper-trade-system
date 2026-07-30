"""Safety-contract tests for the isolated Redis worker benchmark."""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest
import redis
import redis.asyncio as redis_async
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError

from benchmarks.worker_redis import (
    DEFAULT_REDIS_URL,
    InvalidRedisTarget,
    RedisBenchmarkConfig,
    RedisBenchmarkResult,
    run_benchmark,
)
from benchmarks.worker_sqlite import BenchmarkConfig, InvalidBenchmarkConfig, LatencySummary
from benchmarks.worker_redis_runtime import _cleanup_redis
from benchmarks.worker_redis_stream import (
    PositionRef,
    build_cycle_payloads,
    read_group_batch,
)


@pytest.mark.parametrize(
    "redis_url",
    [
        "redis://10.0.0.7:6383/14",
        "redis://127.0.0.1:6383/0", "redis://127.0.0.1:6383/1",
        "redis://127.0.0.1:6383/13", "redis://127.0.0.1:6383/15",
        "redis://127.0.0.1:6379/14", "redis://127.0.0.1:6381/14",
        "redis://localhost:6383/14", "redis://127.0.0.2:6383/14",
        "redis://localhost:6382/14", "redis://127.0.0.1:6398/14",
        "redis://benchmark:secret@127.0.0.1:6383/14",
    ],
)
def test_redis_benchmark_config_rejects_forbidden_target(redis_url: str) -> None:
    # Given a target that could reach a non-isolated or production-like Redis.
    # When the benchmark boundary parses the target.
    with pytest.raises(InvalidRedisTarget):
        _ = RedisBenchmarkConfig(redis_url=redis_url)
    # Then construction fails before any Redis client can be created.


@pytest.mark.parametrize(
    ("field", "values"),
    [
        ("alpha_count", (0, 1, 1)),
        ("positions_per_alpha", (1, 0, 1)),
        ("cycles", (1, 1, 0)),
    ],
)
def test_redis_benchmark_config_rejects_nonpositive_workload(
    field: str,
    values: tuple[int, int, int],
) -> None:
    # Given a workload with one non-positive dimension.
    alpha_count, positions_per_alpha, cycles = values
    # When the shared workload contract is constructed.
    with pytest.raises(InvalidBenchmarkConfig) as captured:
        _ = BenchmarkConfig(
            alpha_count=alpha_count,
            positions_per_alpha=positions_per_alpha,
            cycles=cycles,
        )
    # Then the error identifies the unsafe dimension.
    assert captured.value.field == field


@pytest.mark.parametrize(
    "redis_url", ["redis://127.0.0.1:6383/14?", "redis://127.0.0.1:6383/14#"],
)
def test_redis_benchmark_config_rejects_raw_empty_delimiter(redis_url: str) -> None:
    # Given an otherwise safe URL with an empty query or fragment delimiter.
    # When the target boundary parses it.
    with pytest.raises(InvalidRedisTarget):
        _ = RedisBenchmarkConfig(redis_url=redis_url)
    # Then the ambiguous raw delimiter is rejected before connection.


@pytest.mark.parametrize("control_code", [*range(32), 127])
def test_redis_benchmark_config_rejects_ascii_control(control_code: int) -> None:
    # Given every ASCII control character embedded in an otherwise safe URL.
    redis_url = f"redis://127.0.0.1:6383/14{chr(control_code)}"
    # When the target boundary parses it.
    with pytest.raises(InvalidRedisTarget):
        _ = RedisBenchmarkConfig(redis_url=redis_url)
    # Then construction fails before urlsplit can normalize the input.


def test_redis_benchmark_config_generates_isolated_namespace() -> None:
    # Given the safe default target and a positive workload.
    workload = BenchmarkConfig(alpha_count=2, positions_per_alpha=3, cycles=1)
    # When two independent benchmark configurations are constructed.
    first = RedisBenchmarkConfig(workload=workload)
    second = RedisBenchmarkConfig(workload=workload)
    # Then both targets are parsed and every Redis name is unique and benchmark-owned.
    assert first.redis_url == DEFAULT_REDIS_URL
    assert (first.target.host, first.target.port, first.target.database) == (
        "127.0.0.1",
        6383,
        14,
    )
    assert first.workload == workload
    assert first.namespace != second.namespace
    for name in (
        first.namespace.stream,
        first.namespace.group,
        first.namespace.consumer,
    ):
        assert name.startswith("benchmark:worker:")
        assert name not in {"paper-signals", "paper-executor", "worker-1"}


def test_redis_benchmark_result_serializes_complete_metric_contract() -> None:
    # Given end-to-end counters, timings, state and latency summaries.
    latency = LatencySummary(count=8, avg=1.5, max=3.0, p50=1.0, p95=2.5, p99=3.0)
    result = RedisBenchmarkResult(
        isolated=True,
        scope="worker-redis-stream",
        clock_source="redis-stream-id/server-ms + local-wall-clock-ms",
        alpha_count=2,
        positions_per_alpha=2,
        cycles=1,
        signal_count=8,
        published_count=8,
        received_count=8,
        committed_count=8,
        acked_count=8,
        trade_count=4,
        open_position_count=4,
        error_count=0,
        backlog_start=0,
        backlog_peak=8,
        backlog_final=0,
        final_lag=0,
        final_pending=0,
        produce_duration_sec=0.01,
        drain_duration_sec=0.02,
        total_duration_sec=0.03,
        throughput_signals_per_sec=266.67,
        queue_latency_ms=latency,
        commit_latency_ms=latency,
        ack_latency_ms=latency,
    )
    # When the CLI-facing result is serialized.
    payload = json.loads(result.to_json())
    # Then every benchmark metric is present with structured latency values.
    assert payload == asdict(result)


def test_cycle_payloads_build_all_closes_before_replacement_opens() -> None:
    # Given deterministic current positions and wall-clock bounds for payload creation.
    positions = [PositionRef(0, 0, "position-0"), PositionRef(0, 1, "position-1")]
    before = datetime.now(UTC)
    # When one cycle's publish payloads are constructed.
    signals, replacements = build_cycle_payloads(positions, cycle=2)
    after = datetime.now(UTC)
    # Then emitted order and timestamps preserve CLOSE-before-OPEN semantics.
    assert [signal["type"] for signal in signals] == ["CLOSE", "CLOSE", "OPEN", "OPEN"]
    timestamps = [datetime.fromisoformat(signal["timestamp"]) for signal in signals]
    assert max(timestamps[:2]) <= min(timestamps[2:])
    assert all(before <= timestamp <= after for timestamp in timestamps)
    assert [item.position_id for item in replacements] == [
        "benchmark-position-cycle-2-0-0", "benchmark-position-cycle-2-0-1",
    ]


def test_redis_benchmark_runs_real_stream_to_sqlite_pipeline(tmp_path: Path) -> None:
    # Given a disposable Redis endpoint and a small CLOSE-to-OPEN workload.
    redis_url = os.environ.get("REDIS_BENCHMARK_URL")
    if redis_url is None:
        pytest.skip("REDIS_BENCHMARK_URL is required for the real Redis integration")
    config = RedisBenchmarkConfig(
        redis_url=redis_url,
        workload=BenchmarkConfig(alpha_count=2, positions_per_alpha=2, cycles=1),
    )

    # When the benchmark drives the real stream consumer and SQLite worker core.
    result = anyio.run(run_benchmark, config, tmp_path)

    # Then every published signal commits and acknowledges, with no residual state.
    assert result.isolated is True
    assert result.scope == "worker-redis-stream"
    assert result.signal_count == 8
    assert result.trade_count == 4
    assert result.open_position_count == 4
    assert result.error_count == 0
    assert (
        result.published_count,
        result.received_count,
        result.committed_count,
        result.acked_count,
    ) == (8, 8, 8, 8)
    assert result.backlog_start >= 0
    assert result.backlog_peak >= result.backlog_start
    assert result.backlog_final == 0
    assert result.final_lag == 0
    assert result.final_pending == 0
    assert result.produce_duration_sec >= 0.0
    assert result.drain_duration_sec >= 0.0
    assert result.total_duration_sec > 0.0
    assert result.throughput_signals_per_sec > 0.0
    for latency in (
        result.queue_latency_ms,
        result.commit_latency_ms,
        result.ack_latency_ms,
    ):
        assert latency.count == 8
        assert latency.avg >= 0.0
        assert latency.max >= 0.0
        assert latency.p50 >= 0.0
        assert latency.p95 >= 0.0
        assert latency.p99 >= 0.0

    client = redis.Redis.from_url(redis_url, decode_responses=True)
    try:
        assert client.exists(config.namespace.stream) == 0
    finally:
        client.close()
    assert list(tmp_path.iterdir()) == []


def test_redis_benchmark_connection_failure_is_bounded_and_cleans_temp_db(
    tmp_path: Path,
) -> None:
    # Given the owned port proven free on IPv6 while the integration Redis is IPv4-only.
    try:
        with socket.socket(socket.AF_INET6) as probe:
            probe.bind(("::1", 6383))
    except OSError as exc:
        pytest.skip(f"IPv6 loopback port 6383 is unavailable: {exc}")
    config = RedisBenchmarkConfig(redis_url="redis://[::1]:6383/14")

    # When startup cannot connect to Redis.
    started = time.perf_counter()
    with pytest.raises(RedisConnectionError):
        _ = anyio.run(run_benchmark, config, tmp_path)

    # Then failure is bounded and the temporary SQLite directory is removed.
    assert time.perf_counter() - started < 2.0
    assert list(tmp_path.iterdir()) == []


def test_redis_cleanup_deletes_stream_when_group_destroy_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a real generated stream and a forced XGROUP destroy response error.
    redis_url = os.environ.get("REDIS_BENCHMARK_URL")
    if redis_url is None:
        pytest.skip("REDIS_BENCHMARK_URL is required for the real Redis integration")
    config = RedisBenchmarkConfig(redis_url=redis_url)

    async def exercise_cleanup() -> None:
        client = redis_async.Redis.from_url(redis_url, decode_responses=True)
        await client.xadd(config.namespace.stream, {"type": "OPEN"})

        async def fail_group_destroy(*_args: str) -> bool:
            raise ResponseError("forced missing benchmark group")

        monkeypatch.setattr(client, "xgroup_destroy", fail_group_destroy)
        await _cleanup_redis(client, config, group_created=True)

    # When cleanup attempts group destroy before stream deletion.
    anyio.run(exercise_cleanup)

    # Then the destroy failure does not suppress deletion of the generated stream.
    client = redis.Redis.from_url(redis_url, decode_responses=True)
    try:
        assert client.exists(config.namespace.stream) == 0
    finally:
        client.close()


def test_redis_read_batch_uses_production_count_against_real_stream() -> None:
    # Given 150 messages in a generated stream owned by a real Redis group.
    redis_url = os.environ.get("REDIS_BENCHMARK_URL")
    if redis_url is None:
        pytest.skip("REDIS_BENCHMARK_URL is required for the real Redis integration")
    config = RedisBenchmarkConfig(redis_url=redis_url)

    async def read_once() -> int:
        client = redis_async.Redis.from_url(redis_url, decode_responses=True)
        try:
            await client.xgroup_create(
                config.namespace.stream, config.namespace.group, id="0-0", mkstream=True,
            )
            for index in range(150):
                await client.xadd(config.namespace.stream, {"index": str(index)})
            messages = await read_group_batch(client, config)
            return sum(len(entries) for _, entries in messages)
        finally:
            await _cleanup_redis(client, config, group_created=True)

    # When one production-shaped read is issued.
    batch_count = anyio.run(read_once)
    # Then Redis returns the configured production batch of 100 for serial processing.
    assert batch_count == 100


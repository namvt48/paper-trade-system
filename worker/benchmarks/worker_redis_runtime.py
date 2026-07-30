"""Drive the worker's Redis Stream and SQLite core inside disposable resources.

Only names sealed by :mod:`worker_redis` are created or removed. MDS, fill RPC,
snapshots, and production worker background tasks are intentionally out of scope.
"""

from __future__ import annotations

import inspect
import logging
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import anyio
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.db import Database
from app.executor import Executor

from .worker_redis_models import RedisBenchmarkConfig, RedisBenchmarkResult
from .worker_redis_stream import (
    COMMAND_TIMEOUT_SEC,
    drain_cycle,
    group_state,
    publish_cycle,
    seed_positions,
    summarize,
)

logger = logging.getLogger(__name__)


async def _cleanup_redis(client: Redis, config: RedisBenchmarkConfig, group_created: bool) -> None:
    """Best-effort removal is limited to this run's generated group and stream."""
    if group_created:
        with anyio.move_on_after(COMMAND_TIMEOUT_SEC, shield=True):
            try:
                await client.xgroup_destroy(config.namespace.stream, config.namespace.group)
            except RedisError as exc:
                logger.debug("benchmark group cleanup unavailable: %s", exc)
    with anyio.move_on_after(COMMAND_TIMEOUT_SEC, shield=True):
        try:
            await client.delete(config.namespace.stream)
        except RedisError as exc:
            logger.debug("benchmark stream cleanup unavailable: %s", exc)
    with anyio.move_on_after(COMMAND_TIMEOUT_SEC, shield=True):
        try:
            await client.aclose()
        except RedisError as exc:
            logger.debug("benchmark client cleanup unavailable: %s", exc)


async def run_benchmark(
    config: RedisBenchmarkConfig,
    temp_parent: Path | None = None,
) -> RedisBenchmarkResult:
    """Measure isolated Redis enqueue-to-ack latency and committed SQLite state."""
    client = Redis.from_url(
        config.redis_url,
        decode_responses=True,
        socket_connect_timeout=0.5,
        socket_timeout=1.0,
    )
    group_created = False
    with TemporaryDirectory(prefix="paper-worker-redis-benchmark-", dir=temp_parent) as temp_dir:
        database = Database(str(Path(temp_dir) / "benchmark.db"))
        try:
            await database.init()
            executor = Executor(database, slippage_pct=0.0)
            with anyio.fail_after(COMMAND_TIMEOUT_SEC):
                ping_result = client.ping()
                if inspect.isawaitable(ping_result):
                    await ping_result
                await client.xgroup_create(
                    config.namespace.stream,
                    config.namespace.group,
                    id="0-0",
                    mkstream=True,
                )
            group_created = True
            positions = await seed_positions(database, executor, config)
            initial = await group_state(client, config)
            backlog_peak = initial.backlog
            published = received = committed = acked = errors = 0
            produce_duration = drain_duration = 0.0
            queue_latencies: list[float] = []
            commit_latencies: list[float] = []
            ack_latencies: list[float] = []
            total_started = time.perf_counter()
            for cycle in range(config.workload.cycles):
                produce_started = time.perf_counter()
                cycle_count, positions = await publish_cycle(
                    client, config, positions, cycle,
                )
                produce_duration += time.perf_counter() - produce_started
                published += cycle_count
                drain_started = time.perf_counter()
                backlog_peak = max(backlog_peak, (await group_state(client, config)).backlog)
                cycle_result = await drain_cycle(
                    client, database, executor, config, cycle_count,
                )
                drain_duration += time.perf_counter() - drain_started
                received += cycle_result.received
                committed += cycle_result.committed
                acked += cycle_result.acked
                errors += cycle_result.errors
                queue_latencies.extend(cycle_result.queue_latencies)
                commit_latencies.extend(cycle_result.commit_latencies)
                ack_latencies.extend(cycle_result.ack_latencies)
            final = await group_state(client, config)
            total_duration = time.perf_counter() - total_started
            open_positions = await database.get_all_open_positions()
            trade_count = 0
            for alpha_index in range(config.workload.alpha_count):
                trades = await database.get_trades_by_alpha(
                    f"benchmark-alpha-{alpha_index:03d}",
                    limit=config.workload.positions_per_alpha * config.workload.cycles,
                )
                trade_count += len(trades)
            return RedisBenchmarkResult(
                isolated=True,
                scope="worker-redis-stream",
                clock_source=(
                    "Redis stream-ID server ms to local wall-clock ms; perf_counter durations; "
                    "drain is summed per cycle from last XADD through final XACK"
                ),
                alpha_count=config.workload.alpha_count,
                positions_per_alpha=config.workload.positions_per_alpha,
                cycles=config.workload.cycles,
                signal_count=published,
                published_count=published,
                received_count=received,
                committed_count=committed,
                acked_count=acked,
                trade_count=trade_count,
                open_position_count=len(open_positions),
                error_count=errors,
                backlog_start=initial.backlog,
                backlog_peak=backlog_peak,
                backlog_final=final.backlog,
                final_lag=final.lag,
                final_pending=final.pending,
                produce_duration_sec=produce_duration,
                drain_duration_sec=drain_duration,
                total_duration_sec=total_duration,
                throughput_signals_per_sec=published / total_duration,
                queue_latency_ms=summarize(queue_latencies),
                commit_latency_ms=summarize(commit_latencies),
                ack_latency_ms=summarize(ack_latencies),
            )
        finally:
            try:
                await _cleanup_redis(client, config, group_created)
            finally:
                with anyio.move_on_after(COMMAND_TIMEOUT_SEC, shield=True):
                    await database.close()

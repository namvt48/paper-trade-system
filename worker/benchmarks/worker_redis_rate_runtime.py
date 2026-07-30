"""Orchestrate the sustained-rate benchmark in disposable Redis/SQLite state.

Only the generated benchmark stream and group are removed. The runtime never
uses worker settings, production names, MDS, fill RPC, or global Redis cleanup.
"""

from __future__ import annotations

import inspect
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import anyio
from redis.asyncio import Redis

from app.db import Database
from app.executor import Executor

from .worker_redis_rate_models import RateBenchmarkConfig, RateBenchmarkResult
from .worker_redis_rate_stream import build_rate_payloads, run_concurrently
from .worker_redis_runtime import _cleanup_redis
from .worker_redis_stream import (
    COMMAND_TIMEOUT_SEC,
    group_state,
    seed_positions,
    summarize,
)


async def _query_final_state(
    database: Database,
    config: RateBenchmarkConfig,
) -> tuple[int, int]:
    """Count replacement positions and committed closes in the temporary DB."""
    open_positions = await database.get_all_open_positions()
    trade_count = 0
    for alpha_index in range(config.redis.workload.alpha_count):
        trades = await database.get_trades_by_alpha(
            f"benchmark-alpha-{alpha_index:03d}",
            limit=(
                config.redis.workload.positions_per_alpha * config.redis.workload.cycles
            ),
        )
        trade_count += len(trades)
    return len(open_positions), trade_count


async def run_rate_benchmark(
    config: RateBenchmarkConfig,
    temp_parent: Path | None = None,
) -> RateBenchmarkResult:
    """Measure a paced XADD producer against the real serial commit/XACK path."""
    client = Redis.from_url(
        config.redis.redis_url,
        decode_responses=True,
        socket_connect_timeout=0.5,
        socket_timeout=1.0,
    )
    group_created = False
    with TemporaryDirectory(
        prefix="paper-worker-redis-rate-", dir=temp_parent
    ) as temp_dir:
        database = Database(str(Path(temp_dir) / "benchmark.db"))
        try:
            await database.init()
            executor = Executor(database, slippage_pct=0.0)
            with anyio.fail_after(COMMAND_TIMEOUT_SEC):
                ping_result = client.ping()
                if inspect.isawaitable(ping_result):
                    await ping_result
                await client.xgroup_create(
                    config.redis.namespace.stream,
                    config.redis.namespace.group,
                    id="0-0",
                    mkstream=True,
                )
            group_created = True
            positions = await seed_positions(database, executor, config.redis)
            signals, _ = build_rate_payloads(
                positions,
                config.redis.workload.cycles,
            )
            started = time.perf_counter()
            with anyio.fail_after(120.0):
                state = await run_concurrently(
                    client,
                    database,
                    executor,
                    config,
                    signals,
                )
            total_duration = time.perf_counter() - started
            final = await group_state(client, config.redis)
            open_count, trade_count = await _query_final_state(database, config)
            produce_duration = state.producer_finished - state.produce_started
            producer_backlog = state.producer_end_pending + state.producer_end_lag
            signal_count = len(signals)
            return RateBenchmarkResult(
                isolated=True,
                scope="worker-redis-sustained-rate",
                clock_source=(
                    "Redis stream-ID server ms to local wall-clock queue/e2e ms; "
                    "perf_counter producer schedule, service, XACK RTT, and durations"
                ),
                alpha_count=config.redis.workload.alpha_count,
                positions_per_alpha=config.redis.workload.positions_per_alpha,
                cycles=config.redis.workload.cycles,
                offered_rate=config.offered_rate,
                achieved_produce_rate=state.published / produce_duration,
                signal_count=signal_count,
                published_count=state.published,
                received_count=state.received,
                committed_count=state.committed,
                acked_count=state.acked,
                error_count=state.errors,
                trade_count=trade_count,
                open_position_count=open_count,
                producer_finished_acked_count=state.producer_finished_acked,
                producer_end_pending=state.producer_end_pending,
                producer_end_lag=state.producer_end_lag,
                producer_end_backlog=producer_backlog,
                producer_end_backlog_ratio=producer_backlog / signal_count,
                final_pending=final.pending,
                final_lag=final.lag,
                final_backlog=final.backlog,
                produce_duration_sec=produce_duration,
                total_duration_sec=total_duration,
                post_producer_drain_duration_sec=max(
                    0.0,
                    state.consumer_finished - state.producer_finished,
                ),
                consumer_throughput_signals_per_sec=state.received / total_duration,
                queue_latency_ms=summarize(state.queue_latencies),
                end_to_end_latency_ms=summarize(state.end_to_end_latencies),
                service_latency_ms=summarize(state.service_latencies),
                xack_rtt_ms=summarize(state.xack_latencies),
            )
        finally:
            try:
                await _cleanup_redis(client, config.redis, group_created)
            finally:
                with anyio.move_on_after(COMMAND_TIMEOUT_SEC, shield=True):
                    await database.close()

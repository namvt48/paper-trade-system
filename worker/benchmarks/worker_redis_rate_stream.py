"""Pace Redis publication concurrently with the real serial worker pipeline.

This module owns measured producer/consumer work only. Resource creation and
bounded cleanup remain in :mod:`worker_redis_rate_runtime`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import anyio
from redis.asyncio import Redis
from redis.typing import EncodableT, FieldT

from app.db import Database
from app.executor import Executor
from app.main import process_signal_message

from .worker_redis_rate_models import RateBenchmarkConfig
from .worker_redis_stream import (
    PositionRef,
    SignalPayload,
    build_cycle_payloads,
    group_state,
    read_group_batch,
)


@dataclass(slots=True)  # noqa: MUTABLE_OK
class RateRunState:
    """Accumulate counters and timings shared by two event-loop tasks."""

    published: int = 0
    received: int = 0
    committed: int = 0
    acked: int = 0
    errors: int = 0
    producer_finished_acked: int = 0
    producer_end_pending: int = 0
    producer_end_lag: int = 0
    produce_started: float = 0.0
    producer_finished: float = 0.0
    consumer_finished: float = 0.0
    queue_latencies: list[float] = field(default_factory=list)
    end_to_end_latencies: list[float] = field(default_factory=list)
    service_latencies: list[float] = field(default_factory=list)
    xack_latencies: list[float] = field(default_factory=list)


def build_rate_payloads(
    positions: list[PositionRef],
    cycles: int,
) -> tuple[list[SignalPayload], list[PositionRef]]:
    """Build consecutive CLOSE-before-OPEN cycles and final position references."""
    signals: list[SignalPayload] = []
    current = positions
    for cycle in range(cycles):
        cycle_signals, current = build_cycle_payloads(current, cycle)
        signals.extend(cycle_signals)
    return signals, current


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


async def publish_at_rate(
    client: Redis,
    config: RateBenchmarkConfig,
    signals: list[SignalPayload],
    snapshot_lock: anyio.Lock,
    state: RateRunState,
) -> None:
    """XADD signals against a perf-counter schedule and snapshot producer backlog."""
    state.produce_started = time.perf_counter()
    for index, signal in enumerate(signals):
        target = state.produce_started + index / config.offered_rate
        delay = target - time.perf_counter()
        if delay > 0.0:
            await anyio.sleep(delay)
        fields: dict[FieldT, EncodableT] = {
            **signal,
            "timestamp": _utc_timestamp(),
        }
        if index == len(signals) - 1:
            async with snapshot_lock:
                await client.xadd(config.redis.namespace.stream, fields)
                state.published += 1
                state.producer_finished = time.perf_counter()
                snapshot = await group_state(client, config.redis)
                state.producer_finished_acked = state.acked
                state.producer_end_pending = snapshot.pending
                state.producer_end_lag = snapshot.lag
        else:
            await client.xadd(config.redis.namespace.stream, fields)
            state.published += 1


async def consume_serially(
    client: Redis,
    database: Database,
    executor: Executor,
    config: RateBenchmarkConfig,
    expected: int,
    ready: anyio.Event,
    snapshot_lock: anyio.Lock,
    state: RateRunState,
) -> None:
    """Receive batch-100 entries but commit and acknowledge each one serially."""
    ready.set()
    while state.received < expected:
        messages = await read_group_batch(client, config.redis)
        if not messages:
            continue
        for _, entries in messages:
            for message_id, data in entries:
                async with snapshot_lock:
                    received_wall_ms = time.time_ns() / 1_000_000
                    server_ms = int(message_id.partition("-")[0])
                    state.queue_latencies.append(max(0.0, received_wall_ms - server_ms))
                    state.received += 1
                    service_started = time.perf_counter()
                    result = await process_signal_message(data, database, executor)
                    state.service_latencies.append(
                        (time.perf_counter() - service_started) * 1000.0,
                    )
                    if result is None:
                        state.errors += 1
                    else:
                        state.committed += 1
                    ack_started = time.perf_counter()
                    state.acked += int(
                        await client.xack(
                            config.redis.namespace.stream,
                            config.redis.namespace.group,
                            message_id,
                        ),
                    )
                    state.xack_latencies.append(
                        (time.perf_counter() - ack_started) * 1000.0,
                    )
                    state.end_to_end_latencies.append(
                        max(0.0, time.time_ns() / 1_000_000 - server_ms),
                    )
    state.consumer_finished = time.perf_counter()


async def run_concurrently(
    client: Redis,
    database: Database,
    executor: Executor,
    config: RateBenchmarkConfig,
    signals: list[SignalPayload],
) -> RateRunState:
    """Start the waiting consumer before the paced producer in one task group."""
    ready = anyio.Event()
    snapshot_lock = anyio.Lock()
    state = RateRunState()
    async with anyio.create_task_group() as task_group:
        task_group.start_soon(
            consume_serially,
            client,
            database,
            executor,
            config,
            len(signals),
            ready,
            snapshot_lock,
            state,
        )
        await ready.wait()
        task_group.start_soon(
            publish_at_rate,
            client,
            config,
            signals,
            snapshot_lock,
            state,
        )
    return state

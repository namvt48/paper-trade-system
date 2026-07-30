"""Own deterministic Redis workload generation and serial worker consumption.

The functions here operate only on a namespace already sealed by the isolated
configuration boundary; orchestration and resource cleanup live in runtime.py.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, override

import anyio
from redis.asyncio import Redis
from redis.typing import EncodableT, FieldT

from app.db import Database
from app.executor import Executor
from app.main import process_signal_message

from .worker_redis_models import RedisBenchmarkConfig
from .worker_sqlite import LatencySummary

COMMAND_TIMEOUT_SEC: Final = 2.0
_DRAIN_TIMEOUT_SEC: Final = 60.0
READ_BATCH_SIZE: Final = 100
SignalPayload = dict[str, str]
StreamBatch = list[tuple[str, list[tuple[str, SignalPayload]]]]


@dataclass(frozen=True, slots=True)
class SeedCommitError(RuntimeError):
    """Identify the deterministic seed signal that the worker rejected."""

    signal_id: str

    @override
    def __str__(self) -> str:
        return f"benchmark seed was not committed: {self.signal_id}"


@dataclass(frozen=True, slots=True)
class PositionRef:
    """Identify the deterministic position replaced by the next rebalance."""

    alpha_index: int
    position_index: int
    position_id: str


@dataclass(frozen=True, slots=True)
class GroupState:
    """Report consumer-group work split into delivered and undelivered entries."""

    pending: int
    lag: int

    @property
    def backlog(self) -> int:
        """Count all entries not yet acknowledged by the benchmark group."""
        return self.pending + self.lag


@dataclass(frozen=True, slots=True)
class DrainResult:
    """Collect serial consumer counters and end-to-end latency samples."""

    received: int
    committed: int
    acked: int
    errors: int
    queue_latencies: tuple[float, ...]
    commit_latencies: tuple[float, ...]
    ack_latencies: tuple[float, ...]


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _open_signal(alpha_index: int, position_index: int, generation: str) -> SignalPayload:
    alpha_id = f"benchmark-alpha-{alpha_index:03d}"
    return {
        "type": "OPEN",
        "alpha_id": alpha_id,
        "signal_id": f"benchmark-{generation}-{alpha_index}-{position_index}-open",
        "position_id": f"benchmark-position-{generation}-{alpha_index}-{position_index}",
        "symbol": f"BENCH{position_index:04d}USDT",
        "side": "LONG" if position_index % 2 == 0 else "SHORT",
        "entry": "100.0",
        "qty": "1.0",
        "leverage": "1",
        "timestamp": _timestamp(),
    }


def _close_signal(position: PositionRef, cycle: int) -> SignalPayload:
    return {
        "type": "CLOSE",
        "alpha_id": f"benchmark-alpha-{position.alpha_index:03d}",
        "signal_id": (
            f"benchmark-cycle-{cycle}-{position.alpha_index}-"
            f"{position.position_index}-close"
        ),
        "position_id": position.position_id,
        "reason": "REBALANCE",
        "exit_price": "101.0",
        "timestamp": _timestamp(),
    }


def summarize(samples: list[float]) -> LatencySummary:
    """Summarize samples with the benchmark's nearest-rank percentile contract."""
    ordered = sorted(samples)
    if not ordered:
        return LatencySummary(count=0, avg=0.0, max=0.0, p50=0.0, p95=0.0, p99=0.0)

    def nearest_rank(quantile: float) -> float:
        return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]

    return LatencySummary(
        count=len(ordered),
        avg=sum(ordered) / len(ordered),
        max=ordered[-1],
        p50=nearest_rank(0.50),
        p95=nearest_rank(0.95),
        p99=nearest_rank(0.99),
    )


async def group_state(client: Redis, config: RedisBenchmarkConfig) -> GroupState:
    """Read lag and pending from the owned group rather than stream length."""
    with anyio.fail_after(COMMAND_TIMEOUT_SEC):
        groups = await client.xinfo_groups(config.namespace.stream)
    group = next(item for item in groups if item["name"] == config.namespace.group)
    return GroupState(
        pending=int(group.get("pending") or 0),
        lag=int(group.get("lag") or 0),
    )


async def seed_positions(
    database: Database,
    executor: Executor,
    config: RedisBenchmarkConfig,
) -> list[PositionRef]:
    """Create deterministic starting positions outside all timed counters."""
    positions: list[PositionRef] = []
    for alpha_index in range(config.workload.alpha_count):
        for position_index in range(config.workload.positions_per_alpha):
            signal = _open_signal(alpha_index, position_index, "seed")
            result = await process_signal_message(signal, database, executor)
            if result is None:
                raise SeedCommitError(signal_id=signal["signal_id"])
            positions.append(PositionRef(alpha_index, position_index, signal["position_id"]))
    return positions


async def publish_cycle(
    client: Redis,
    config: RedisBenchmarkConfig,
    positions: list[PositionRef],
    cycle: int,
) -> tuple[int, list[PositionRef]]:
    """Append every CLOSE before any replacement OPEN for one rebalance cycle."""
    signals, replacements = build_cycle_payloads(positions, cycle)
    with anyio.fail_after(_DRAIN_TIMEOUT_SEC):
        for signal in signals:
            fields: dict[FieldT, EncodableT] = {}
            for key, value in signal.items():
                fields[key] = value
            await client.xadd(config.namespace.stream, fields)
    return len(signals), replacements


def build_cycle_payloads(
    positions: list[PositionRef],
    cycle: int,
) -> tuple[list[SignalPayload], list[PositionRef]]:
    """Build every current-position CLOSE before constructing replacement OPENs."""
    close_signals = [_close_signal(item, cycle) for item in positions]
    replacement_signals = [
        _open_signal(item.alpha_index, item.position_index, f"cycle-{cycle}")
        for item in positions
    ]
    replacements = [
        PositionRef(item.alpha_index, item.position_index, signal["position_id"])
        for item, signal in zip(positions, replacement_signals, strict=True)
    ]
    return [*close_signals, *replacement_signals], replacements


async def read_group_batch(client: Redis, config: RedisBenchmarkConfig) -> StreamBatch:
    """Read up to the production worker's 100-entry batch from the owned group."""
    return await client.xreadgroup(
        config.namespace.group,
        config.namespace.consumer,
        {config.namespace.stream: ">"},
        count=READ_BATCH_SIZE,
        block=500,
    )


async def drain_cycle(
    client: Redis,
    database: Database,
    executor: Executor,
    config: RedisBenchmarkConfig,
    expected: int,
) -> DrainResult:
    """Consume serially through the real commit and acknowledgement boundary."""
    received = committed = acked = errors = 0
    queue_latencies: list[float] = []
    commit_latencies: list[float] = []
    ack_latencies: list[float] = []
    with anyio.fail_after(_DRAIN_TIMEOUT_SEC):
        while received < expected:
            messages = await read_group_batch(client, config)
            if not messages:
                continue
            for _, entries in messages:
                for message_id, data in entries:
                    server_ms = int(message_id.partition("-")[0])
                    queue_latencies.append(max(0.0, time.time_ns() / 1_000_000 - server_ms))
                    received += 1
                    result = await process_signal_message(data, database, executor)
                    commit_latencies.append(max(0.0, time.time_ns() / 1_000_000 - server_ms))
                    if result is None:
                        errors += 1
                    else:
                        committed += 1
                    acked += int(
                        await client.xack(
                            config.namespace.stream,
                            config.namespace.group,
                            message_id,
                        ),
                    )
                    ack_latencies.append(max(0.0, time.time_ns() / 1_000_000 - server_ms))
    return DrainResult(
        received,
        committed,
        acked,
        errors,
        tuple(queue_latencies),
        tuple(commit_latencies),
        tuple(ack_latencies),
    )

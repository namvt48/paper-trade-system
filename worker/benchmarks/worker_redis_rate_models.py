"""Define the sealed sustained-rate workload and redacted result contract.

The result intentionally excludes Redis target details and generated names so
saved benchmark evidence cannot disclose or accidentally reuse a connection.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from typing import override

from .worker_redis_models import RedisBenchmarkConfig
from .worker_sqlite import LatencySummary


@dataclass(frozen=True, slots=True)
class InvalidOfferedRate(ValueError):
    """Identify a non-finite or non-positive producer rate at the CLI boundary."""

    value: float

    @override
    def __str__(self) -> str:
        """Render a target-free error suitable for argparse."""
        return f"offered_rate must be finite and positive, got {self.value}"


@dataclass(frozen=True, slots=True)
class RateBenchmarkConfig:
    """Pair a sealed Redis workload with a target signals-per-second schedule."""

    redis: RedisBenchmarkConfig
    offered_rate: float

    def __post_init__(self) -> None:
        """Reject schedules that cannot advance monotonically."""
        if not math.isfinite(self.offered_rate) or self.offered_rate <= 0.0:
            raise InvalidOfferedRate(value=self.offered_rate)


@dataclass(frozen=True, slots=True)
class RateBenchmarkResult:
    """Expose paced producer, serial consumer, backlog, and SQLite evidence."""

    isolated: bool
    scope: str
    clock_source: str
    alpha_count: int
    positions_per_alpha: int
    cycles: int
    offered_rate: float
    achieved_produce_rate: float
    signal_count: int
    published_count: int
    received_count: int
    committed_count: int
    acked_count: int
    error_count: int
    trade_count: int
    open_position_count: int
    producer_finished_acked_count: int
    producer_end_pending: int
    producer_end_lag: int
    producer_end_backlog: int
    producer_end_backlog_ratio: float
    final_pending: int
    final_lag: int
    final_backlog: int
    produce_duration_sec: float
    total_duration_sec: float
    post_producer_drain_duration_sec: float
    consumer_throughput_signals_per_sec: float
    queue_latency_ms: LatencySummary
    end_to_end_latency_ms: LatencySummary
    service_latency_ms: LatencySummary
    xack_rtt_ms: LatencySummary

    def to_json(self) -> str:
        """Serialize stable one-line evidence without target or namespace data."""
        return json.dumps(asdict(self), sort_keys=True)

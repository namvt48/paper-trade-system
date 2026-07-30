"""Seal safe Redis targets, generated namespaces, and benchmark result types.

This boundary never accepts caller-provided stream, group, or consumer names,
and serialized evidence never includes target credentials or connection data.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Final, override
from urllib.parse import urlsplit
from uuid import uuid4

from .worker_sqlite import BenchmarkConfig, LatencySummary

DEFAULT_REDIS_URL: Final = "redis://127.0.0.1:6383/14"
_BENCHMARK_NAMESPACE_PREFIX: Final = "benchmark:worker:"
_OWNED_PORT: Final = 6383
_OWNED_DATABASE: Final = 14
_OWNED_HOSTS: Final = frozenset({"127.0.0.1", "::1"})


@dataclass(frozen=True, slots=True)
class InvalidRedisTarget(ValueError):
    """Describe why a Redis URL is unsafe without exposing URL credentials."""

    reason: str

    @override
    def __str__(self) -> str:
        """Return a credential-free boundary error for CLI reporting."""
        return f"invalid benchmark Redis target: {self.reason}"


@dataclass(frozen=True, slots=True)
class RedisTarget:
    """Hold a parsed loopback Redis endpoint with a non-default database."""

    host: str
    port: int
    database: int


@dataclass(frozen=True, slots=True)
class BenchmarkNamespace:
    """Hold Redis identifiers generated exclusively for one benchmark run."""

    stream: str
    group: str
    consumer: str


@dataclass(frozen=True, slots=True)
class RedisBenchmarkConfig:
    """Validate an isolated target and seal generated names into the workload."""

    redis_url: str = DEFAULT_REDIS_URL
    workload: BenchmarkConfig = BenchmarkConfig()
    target: RedisTarget = field(init=False)
    namespace: BenchmarkNamespace = field(init=False)

    def __post_init__(self) -> None:
        """Parse the target and generate names before the config becomes usable."""
        object.__setattr__(self, "target", _parse_redis_target(self.redis_url))
        root = f"{_BENCHMARK_NAMESPACE_PREFIX}{uuid4().hex}"
        object.__setattr__(
            self,
            "namespace",
            BenchmarkNamespace(
                stream=f"{root}:stream",
                group=f"{root}:group",
                consumer=f"{root}:consumer",
            ),
        )


@dataclass(frozen=True, slots=True)
class RedisBenchmarkResult:
    """Expose the complete stream-to-commit benchmark evidence contract."""

    isolated: bool
    scope: str
    clock_source: str
    alpha_count: int
    positions_per_alpha: int
    cycles: int
    signal_count: int
    published_count: int
    received_count: int
    committed_count: int
    acked_count: int
    trade_count: int
    open_position_count: int
    error_count: int
    backlog_start: int
    backlog_peak: int
    backlog_final: int
    final_lag: int
    final_pending: int
    produce_duration_sec: float
    drain_duration_sec: float
    total_duration_sec: float
    throughput_signals_per_sec: float
    queue_latency_ms: LatencySummary
    commit_latency_ms: LatencySummary
    ack_latency_ms: LatencySummary

    def to_json(self) -> str:
        """Serialize stable CLI evidence without exposing Redis target details."""
        return json.dumps(asdict(self), sort_keys=True)


def _parse_redis_target(redis_url: str) -> RedisTarget:
    """Parse a Redis URL that cannot resolve beyond this host or default DB."""
    if any(ord(character) < 32 or ord(character) == 127 for character in redis_url):
        raise InvalidRedisTarget(reason="ASCII control characters are forbidden")
    if "?" in redis_url or "#" in redis_url:
        raise InvalidRedisTarget(reason="query and fragment delimiters are forbidden")
    try:
        parsed = urlsplit(redis_url)
        port = parsed.port or 6379
    except ValueError as exc:
        raise InvalidRedisTarget(reason="malformed URL") from exc

    if parsed.scheme != "redis" or parsed.query or parsed.fragment:
        raise InvalidRedisTarget(reason="URL must use redis:// without query or fragment")
    if parsed.username is not None or parsed.password is not None:
        raise InvalidRedisTarget(reason="credentials are forbidden")

    host = parsed.hostname
    if host is None:
        raise InvalidRedisTarget(reason="host is required")
    if host not in _OWNED_HOSTS:
        raise InvalidRedisTarget(reason="host must be 127.0.0.1 or ::1")

    if port != _OWNED_PORT:
        raise InvalidRedisTarget(reason=f"port must be {_OWNED_PORT}")

    database_text = parsed.path.removeprefix("/")
    if parsed.path != f"/{database_text}" or not database_text.isdecimal():
        raise InvalidRedisTarget(reason="database path must be one positive integer")
    database = int(database_text)
    if database != _OWNED_DATABASE:
        raise InvalidRedisTarget(reason=f"database must be {_OWNED_DATABASE}")
    return RedisTarget(host=host, port=port, database=database)

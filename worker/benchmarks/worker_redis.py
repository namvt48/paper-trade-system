"""Expose the disposable Redis benchmark's safe API and command-line surface.

Target parsing and result models live in worker_redis_models; runtime and stream
modules separately own orchestration and workload execution.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import anyio

from .worker_redis_models import (
    DEFAULT_REDIS_URL,
    BenchmarkNamespace,
    InvalidRedisTarget,
    RedisBenchmarkConfig,
    RedisBenchmarkResult,
    RedisTarget,
)
from .worker_redis_runtime import run_benchmark
from .worker_sqlite import BenchmarkConfig

__all__ = (
    "DEFAULT_REDIS_URL",
    "BenchmarkNamespace",
    "InvalidRedisTarget",
    "RedisBenchmarkConfig",
    "RedisBenchmarkResult",
    "RedisTarget",
    "run_benchmark",
)


def main() -> None:
    """Parse an isolated workload and print one machine-readable result."""
    parser = argparse.ArgumentParser(description="Benchmark worker Redis-to-SQLite throughput")
    parser.add_argument("--redis-url", default=DEFAULT_REDIS_URL)
    parser.add_argument("--alphas", type=int, default=100)
    parser.add_argument("--positions-per-alpha", type=int, default=10)
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--temp-parent", type=Path, default=None)
    args = parser.parse_args()
    try:
        config = RedisBenchmarkConfig(
            redis_url=args.redis_url,
            workload=BenchmarkConfig(
                alpha_count=args.alphas,
                positions_per_alpha=args.positions_per_alpha,
                cycles=args.cycles,
            ),
        )
    except (InvalidRedisTarget, ValueError) as exc:
        parser.error(str(exc))
    result = anyio.run(run_benchmark, config, args.temp_parent)
    print(result.to_json())


if __name__ == "__main__":
    main()

"""Expose the isolated sustained-rate Redis benchmark CLI and typed API."""

from __future__ import annotations

import argparse
from pathlib import Path

import anyio

from .worker_redis_models import DEFAULT_REDIS_URL, RedisBenchmarkConfig
from .worker_redis_rate_models import (
    InvalidOfferedRate,
    RateBenchmarkConfig,
    RateBenchmarkResult,
)
from .worker_redis_rate_runtime import run_rate_benchmark
from .worker_sqlite import BenchmarkConfig, InvalidBenchmarkConfig

__all__ = (
    "InvalidOfferedRate",
    "RateBenchmarkConfig",
    "RateBenchmarkResult",
    "run_rate_benchmark",
)


def main() -> None:
    """Parse one sealed paced workload and print machine-readable evidence."""
    parser = argparse.ArgumentParser(
        description="Benchmark sustained worker Redis-to-SQLite throughput",
    )
    parser.add_argument("--redis-url", default=DEFAULT_REDIS_URL)
    parser.add_argument("--alphas", type=int, default=100)
    parser.add_argument("--positions-per-alpha", type=int, default=10)
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--offered-rate", type=float, required=True)
    parser.add_argument("--temp-parent", type=Path, default=None)
    args = parser.parse_args()
    try:
        config = RateBenchmarkConfig(
            redis=RedisBenchmarkConfig(
                redis_url=args.redis_url,
                workload=BenchmarkConfig(
                    alpha_count=args.alphas,
                    positions_per_alpha=args.positions_per_alpha,
                    cycles=args.cycles,
                ),
            ),
            offered_rate=args.offered_rate,
        )
    except (InvalidBenchmarkConfig, InvalidOfferedRate, ValueError) as exc:
        parser.error(str(exc))
    result = anyio.run(run_rate_benchmark, config, args.temp_parent)
    print(result.to_json())


if __name__ == "__main__":
    main()

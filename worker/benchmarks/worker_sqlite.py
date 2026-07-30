"""Measure the real worker signal pipeline against an automatically removed DB.

This harness deliberately bypasses Redis so it measures parsing, execution and
SQLite transaction capacity without any route to the production stream. Redis
consumer backlog is a separate benchmark phase and is not claimed here.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import anyio

from app.db import Database
from app.executor import Executor
from app.main import process_signal_message


@dataclass(frozen=True, slots=True)
class InvalidBenchmarkConfig(ValueError):
    """Identify a non-positive workload dimension at the CLI/API boundary."""

    field: str
    value: int

    def __str__(self) -> str:
        return f"{self.field} must be positive, got {self.value}"


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    """Define the size of a synchronized OPEN then CLOSE rebalance workload."""

    alpha_count: int = 100
    positions_per_alpha: int = 10
    cycles: int = 1

    def __post_init__(self) -> None:
        for field, value in (
            ("alpha_count", self.alpha_count),
            ("positions_per_alpha", self.positions_per_alpha),
            ("cycles", self.cycles),
        ):
            if value < 1:
                raise InvalidBenchmarkConfig(field=field, value=value)


@dataclass(frozen=True, slots=True)
class LatencySummary:
    """Report nearest-rank latency quantiles in milliseconds."""

    count: int
    avg: float
    max: float
    p50: float
    p95: float
    p99: float


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    """Describe throughput and final DB state for one disposable benchmark."""

    isolated: bool
    scope: str
    alpha_count: int
    positions_per_alpha: int
    cycles: int
    signal_count: int
    trade_count: int
    open_position_count: int
    error_count: int
    duration_sec: float
    throughput_signals_per_sec: float
    open_latency_ms: LatencySummary
    close_latency_ms: LatencySummary

    def to_json(self) -> str:
        """Serialize the benchmark result for shell evidence capture."""
        return json.dumps(asdict(self), sort_keys=True)


def _nearest_rank(ordered: list[float], quantile: float) -> float:
    rank = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[rank]


def _summarize(samples: list[float]) -> LatencySummary:
    ordered = sorted(samples)
    if not ordered:
        return LatencySummary(count=0, avg=0.0, max=0.0, p50=0.0, p95=0.0, p99=0.0)
    return LatencySummary(
        count=len(ordered),
        avg=sum(ordered) / len(ordered),
        max=ordered[-1],
        p50=_nearest_rank(ordered, 0.50),
        p95=_nearest_rank(ordered, 0.95),
        p99=_nearest_rank(ordered, 0.99),
    )


async def run_benchmark(
    config: BenchmarkConfig,
    temp_parent: Path | None = None,
) -> BenchmarkResult:
    """Run synchronized rebalance bursts using a DB confined to a temp directory."""
    open_latencies: list[float] = []
    close_latencies: list[float] = []
    error_count = 0
    started = time.perf_counter()

    with TemporaryDirectory(prefix="paper-worker-benchmark-", dir=temp_parent) as temp_dir:
        database = Database(str(Path(temp_dir) / "benchmark.db"))
        await database.init()
        executor = Executor(database, slippage_pct=0.0)
        try:
            for cycle in range(config.cycles):
                positions: list[tuple[int, int, str]] = []
                for alpha_index in range(config.alpha_count):
                    for position_index in range(config.positions_per_alpha):
                        open_started = time.perf_counter()
                        result = await process_signal_message(
                            {
                                "type": "OPEN",
                                "alpha_id": f"benchmark-alpha-{alpha_index:03d}",
                                "signal_id": f"benchmark-{cycle}-{alpha_index}-{position_index}-open",
                                "symbol": f"BENCH{position_index:04d}USDT",
                                "side": "LONG" if position_index % 2 == 0 else "SHORT",
                                "entry": "100.0",
                                "qty": "1.0",
                                "leverage": "1",
                                "timestamp": "2026-07-17T00:00:00Z",
                            },
                            database,
                            executor,
                        )
                        open_latencies.append((time.perf_counter() - open_started) * 1000.0)
                        if result is None:
                            error_count += 1
                            continue
                        positions.append((alpha_index, position_index, str(result["position_id"])))

                for alpha_index, position_index, position_id in positions:
                    close_started = time.perf_counter()
                    result = await process_signal_message(
                        {
                            "type": "CLOSE",
                            "alpha_id": f"benchmark-alpha-{alpha_index:03d}",
                            "signal_id": f"benchmark-{cycle}-{alpha_index}-{position_index}-close",
                            "position_id": position_id,
                            "reason": "REBALANCE",
                            "exit_price": "101.0",
                            "timestamp": "2026-07-17T00:15:00Z",
                        },
                        database,
                        executor,
                    )
                    close_latencies.append((time.perf_counter() - close_started) * 1000.0)
                    if result is None:
                        error_count += 1

            open_positions = await database.get_all_open_positions()
            trade_count = 0
            expected_trades = config.positions_per_alpha * config.cycles
            for alpha_index in range(config.alpha_count):
                trades = await database.get_trades_by_alpha(
                    f"benchmark-alpha-{alpha_index:03d}",
                    limit=expected_trades,
                )
                trade_count += len(trades)
        finally:
            await database.close()

    duration_sec = time.perf_counter() - started
    signal_count = len(open_latencies) + len(close_latencies)
    return BenchmarkResult(
        isolated=True,
        scope="worker-sqlite-direct",
        alpha_count=config.alpha_count,
        positions_per_alpha=config.positions_per_alpha,
        cycles=config.cycles,
        signal_count=signal_count,
        trade_count=trade_count,
        open_position_count=len(open_positions),
        error_count=error_count,
        duration_sec=duration_sec,
        throughput_signals_per_sec=signal_count / duration_sec,
        open_latency_ms=_summarize(open_latencies),
        close_latency_ms=_summarize(close_latencies),
    )


def main() -> None:
    """Parse benchmark size, execute it and emit one machine-readable result."""
    parser = argparse.ArgumentParser(description="Benchmark worker SQLite throughput in an isolated temp DB")
    parser.add_argument("--alphas", type=int, default=100)
    parser.add_argument("--positions-per-alpha", type=int, default=10)
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--temp-parent", type=Path, default=None)
    args = parser.parse_args()
    try:
        config = BenchmarkConfig(
            alpha_count=args.alphas,
            positions_per_alpha=args.positions_per_alpha,
            cycles=args.cycles,
        )
    except InvalidBenchmarkConfig as exc:
        parser.error(str(exc))
    result = anyio.run(run_benchmark, config, args.temp_parent)
    print(result.to_json())


if __name__ == "__main__":
    main()

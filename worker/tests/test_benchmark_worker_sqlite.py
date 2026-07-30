"""Integration tests for the disposable worker/SQLite benchmark."""

from __future__ import annotations

import pytest

from benchmarks.worker_sqlite import BenchmarkConfig, run_benchmark


@pytest.mark.asyncio
async def test_benchmark_processes_rebalance_burst_in_disposable_database(tmp_path) -> None:
    config = BenchmarkConfig(alpha_count=2, positions_per_alpha=2, cycles=1)

    result = await run_benchmark(config, temp_parent=tmp_path)

    assert result.isolated is True
    assert result.scope == "worker-sqlite-direct"
    assert result.signal_count == 8
    assert result.trade_count == 4
    assert result.open_position_count == 0
    assert result.error_count == 0
    assert result.throughput_signals_per_sec > 0
    assert result.open_latency_ms.p95 >= 0
    assert result.close_latency_ms.p95 >= 0
    assert list(tmp_path.iterdir()) == []


def test_benchmark_config_rejects_zero_sized_workload() -> None:
    with pytest.raises(ValueError):
        BenchmarkConfig(alpha_count=0, positions_per_alpha=1, cycles=1)

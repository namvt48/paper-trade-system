from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from runner import main as runner_main
from runner.metrics import RunnerMetrics
from runner.metrics_http import MetricsServer


class LeaseStub:
    def __init__(self, failing_alpha):
        self.failing_alpha = failing_alpha
        self.renewed = []

    def renew(self, alpha_id):
        self.renewed.append(alpha_id)
        return alpha_id != self.failing_alpha


def _strategy(alpha_id):
    return SimpleNamespace(ctx=SimpleNamespace(
        alpha_id=alpha_id,
        state=SimpleNamespace(ready=True, lease_valid=True),
    ))


@pytest.mark.asyncio
async def test_lease_renewal_failure_suspends_only_that_strategy():
    stop = asyncio.Event()
    strategies = [_strategy("a1"), _strategy("a2")]
    lease = LeaseStub("a2")

    task = asyncio.create_task(runner_main.renew_strategy_leases(lease, strategies, 0.001, stop))
    await asyncio.sleep(0.01)
    stop.set()
    await task

    assert strategies[0].ctx.state.lease_valid is True
    assert strategies[1].ctx.state.lease_valid is False


@pytest.mark.asyncio
async def test_metrics_http_handlers_return_health_and_snapshot():
    metrics = RunnerMetrics(warmup_cache_hits_total=3)
    server = MetricsServer(lambda: metrics.snapshot())

    health = await server._health(None)
    snapshot = await server._metrics(None)

    assert health.status == 200
    assert snapshot.status == 200
    assert b"warmup_cache_hits_total" in snapshot.body


def test_runner_metrics_snapshot_counts_active_and_suspended():
    cfg = SimpleNamespace(runner_id="r", signal_stream="paper-signals-shadow", shadow_mode=True)
    strategies = [_strategy("a1"), _strategy("a2")]
    strategies[1].ctx.state.lease_valid = False

    snapshot = runner_main.runner_metrics_snapshot(RunnerMetrics(), cfg, strategies, lease=None)

    assert snapshot["strategies_active"] == 1
    assert snapshot["strategies_suspended"] == 1

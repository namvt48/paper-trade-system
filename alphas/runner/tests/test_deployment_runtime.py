from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from runner import main as runner_main
from runner.metrics import RunnerMetrics
from runner.metrics_http import MetricsServer


class LeaseStub:
    """Always reports itself as the current owner; renewal permanently fails for one alpha."""

    def __init__(self, failing_alpha):
        self.failing_alpha = failing_alpha
        self.renewed = []

    def is_valid(self, alpha_id):
        return True

    def renew(self, alpha_id):
        self.renewed.append(alpha_id)
        return alpha_id != self.failing_alpha

    def acquire(self, alpha_id):
        return alpha_id != self.failing_alpha


class RecoveringLeaseStub:
    """Simulates a lease that is lost once (renew fails, ownership drops), then re-acquired."""

    def __init__(self, failing_alpha):
        self.failing_alpha = failing_alpha
        self.owned = {failing_alpha: True}
        self.failed_once = False

    def is_valid(self, alpha_id):
        return self.owned.get(alpha_id, True)

    def renew(self, alpha_id):
        if alpha_id == self.failing_alpha and not self.failed_once:
            self.failed_once = True
            self.owned[alpha_id] = False
            return False
        return True

    def acquire(self, alpha_id):
        self.owned[alpha_id] = True
        return True


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
async def test_lease_renewal_self_heals_after_transient_failure():
    stop = asyncio.Event()
    strategies = [_strategy("a1"), _strategy("a2")]
    lease = RecoveringLeaseStub("a2")

    task = asyncio.create_task(runner_main.renew_strategy_leases(lease, strategies, 0.001, stop))
    await asyncio.sleep(0.1)
    stop.set()
    await task

    assert strategies[0].ctx.state.lease_valid is True
    assert strategies[1].ctx.state.lease_valid is True


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


def _strategy_with_tf(alpha_id, tf):
    strategy = _strategy(alpha_id)
    strategy.get_warmup_tfs = lambda: [tf]
    return strategy


def test_runner_metrics_snapshot_flags_alpha_silent_far_longer_than_its_timeframe(monkeypatch):
    """Regression for the 2026-07-16 incident: an alpha that stops
    processing events entirely must show up in /metrics and /health,
    not just in raw runner logs.
    """
    cfg = SimpleNamespace(runner_id="r", signal_stream="paper-signals-shadow", shadow_mode=True)
    strategies = [_strategy_with_tf("1d-silent", "1d"), _strategy_with_tf("1d-healthy", "1d")]

    now = 10_000_000.0
    metrics = RunnerMetrics()
    metrics.last_event_ts_by_alpha["1d-silent"] = now - (3 * 86_400)  # 3 days silent
    metrics.last_event_ts_by_alpha["1d-healthy"] = now - 60  # just processed a minute ago

    monkeypatch.setattr(runner_main.time, "time", lambda: now)
    snapshot = runner_main.runner_metrics_snapshot(metrics, cfg, strategies, lease=None)

    assert snapshot["stale_alphas"] == ["1d-silent"]
    assert snapshot["last_event_age_sec"]["1d-silent"] == pytest.approx(3 * 86_400)
    assert snapshot["last_event_age_sec"]["1d-healthy"] == pytest.approx(60)


def test_runner_metrics_snapshot_does_not_flag_alpha_that_never_processed_yet():
    """An alpha with no recorded event at all (e.g. just started) must
    not be flagged stale -- there's nothing abnormal about that."""
    cfg = SimpleNamespace(runner_id="r", signal_stream="paper-signals-shadow", shadow_mode=True)
    strategies = [_strategy_with_tf("1d-new", "1d")]

    snapshot = runner_main.runner_metrics_snapshot(RunnerMetrics(), cfg, strategies, lease=None)

    assert snapshot["stale_alphas"] == []


@pytest.mark.asyncio
async def test_health_endpoint_returns_503_when_an_alpha_is_stale():
    snapshot = {"stale_alphas": ["1d-silent"]}
    server = MetricsServer(lambda: snapshot)

    health = await server._health(None)

    assert health.status == 503


def test_setup_logging_emits_to_stdout(monkeypatch):
    """setup_logging should configure a queue-based stdout handler only — no file handlers."""
    runner_main.setup_logging()
    logging.getLogger("test.runner_stdout").info("stdout test message")
    runner_main.shutdown_logging()

    # Verify no file handlers are registered
    root = logging.getLogger()
    for handler in root.handlers:
        assert not isinstance(handler, logging.FileHandler)

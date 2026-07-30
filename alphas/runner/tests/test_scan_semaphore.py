"""Regression test for the concurrency ceiling on handle_strategy_event
(.agents/PLAN.md U5). On 2026-07-16, a single universe refresh fanned out
to ~40 alphas' queues at once; each one's own event-loop task called
handle_strategy_event (and its asyncio.to_thread compute work)
independently and simultaneously, overwhelming the runner's small shared
thread pool in one burst. `run_strategy_event_loop`'s `scan_semaphore`
parameter must cap how many of those calls run at the same instant.
"""
from __future__ import annotations

import asyncio

import pytest

from runner import main as runner_main
from runner.data_layer.cache import SharedCandleCache
from runner.data_layer.pubsub import DataEvent
from runner.metrics import RunnerMetrics
from runner.reconcile.state import StrategyRuntimeState
from runner.strategy.base import Strategy
from runner.strategy.context import StrategyContext


class ConcurrencyTrackingStrategy(Strategy):
    concurrency = 0
    max_concurrency = 0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.release: asyncio.Event | None = None

    def get_required_channels_instance(self) -> list[str]:
        return ["kline:binance:1d"]

    def get_warmup_symbols(self) -> list[str]:
        return ["BTCUSDT"]

    def get_warmup_tfs(self) -> list[str]:
        return ["1d"]

    def get_warmup_bars(self, tf: str) -> int:
        return 2

    async def scan(self) -> None:
        ConcurrencyTrackingStrategy.concurrency += 1
        ConcurrencyTrackingStrategy.max_concurrency = max(
            ConcurrencyTrackingStrategy.max_concurrency, ConcurrencyTrackingStrategy.concurrency,
        )
        try:
            await self.release.wait()
        finally:
            ConcurrencyTrackingStrategy.concurrency -= 1


@pytest.mark.asyncio
async def test_scan_semaphore_bounds_concurrent_scans_during_mass_refresh():
    ConcurrencyTrackingStrategy.concurrency = 0
    ConcurrencyTrackingStrategy.max_concurrency = 0
    release = asyncio.Event()

    n_alphas = 6
    semaphore_size = 2
    sem = asyncio.Semaphore(semaphore_size)

    strategies = []
    queues = []
    for i in range(n_alphas):
        ctx = StrategyContext(f"a{i}", "1", SharedCandleCache(), None, StrategyRuntimeState(ready=True))
        strategy = ConcurrencyTrackingStrategy(alpha_id=f"a{i}", version="1", params={}, ctx=ctx)
        strategy.release = release
        strategies.append(strategy)
        queues.append(asyncio.Queue())

    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(runner_main.run_strategy_event_loop(s, q, stop, None, sem))
        for s, q in zip(strategies, queues)
    ]

    try:
        # Simulate a mass universe refresh: every alpha's queue gets a
        # "symbols" event at once.
        for q in queues:
            await q.put(DataEvent("symbols:binance", "symbols", "", "", {"symbols": []}))

        # Give every strategy's loop a chance to attempt entry.
        await asyncio.sleep(0.2)

        assert ConcurrencyTrackingStrategy.max_concurrency <= semaphore_size, (
            f"expected at most {semaphore_size} concurrent scans, saw "
            f"{ConcurrencyTrackingStrategy.max_concurrency} -- a mass "
            "refresh must not let every alpha hit the shared compute pool "
            "at once (2026-07-16 incident)"
        )
        assert ConcurrencyTrackingStrategy.max_concurrency > 0, "test setup did not exercise any scan"
    finally:
        release.set()
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_run_strategy_event_loop_without_semaphore_still_works():
    """Existing callers that don't pass scan_semaphore (e.g. current
    tests, or any future caller that opts out) must be unaffected."""
    ctx = StrategyContext("a", "1", SharedCandleCache(), None, StrategyRuntimeState(ready=True))

    class ImmediateStrategy(Strategy):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.scanned = False

        def get_required_channels_instance(self) -> list[str]:
            return ["kline:binance:1d"]

        def get_warmup_symbols(self) -> list[str]:
            return ["BTCUSDT"]

        def get_warmup_tfs(self) -> list[str]:
            return ["1d"]

        def get_warmup_bars(self, tf: str) -> int:
            return 2

        async def scan(self) -> None:
            self.scanned = True

    strategy = ImmediateStrategy(alpha_id="a", version="1", params={}, ctx=ctx)
    queue: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()
    metrics = RunnerMetrics()
    task = asyncio.create_task(runner_main.run_strategy_event_loop(strategy, queue, stop, metrics))

    try:
        await queue.put(DataEvent("symbols:binance", "symbols", "", "", {"symbols": []}))
        await asyncio.wait_for(queue.join(), timeout=1.0)
        assert strategy.scanned
        performance = metrics.snapshot()["performance"]
        assert performance["event_total"] == 1
        assert performance["scan_total"] == 1
        assert performance["event_by_kind"] == {"symbols": 1}
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
